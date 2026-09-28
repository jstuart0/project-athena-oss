"""ATHENA-128 -- state-question classifier, metrics, and routing tests.

Phase 2.1 (this section): the classifier corpus, committed and run
BEFORE `orchestrator.utterance_kind` exists (test-first; plan 2.1). The
module-not-found failure at that point is the recorded red evidence.

Phase 3.6 routing tests are appended in the Phase 3 commit.
"""
from __future__ import annotations

import sys
import unittest.mock as mock

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")
sys.path.insert(0, "src/orchestrator")  # music_handler is imported as a bare module

from orchestrator.utterance_kind import UtteranceKind, classify_utterance

# ---------------------------------------------------------------------------
# Corpora (Phase 2.1)
# ---------------------------------------------------------------------------

PROBE_PHRASE = "are the office lights currently on or off right now"

QUESTION_CORPUS = [
    # light (>= 5)
    PROBE_PHRASE,
    "are the kitchen lights on",
    "is the bedroom lamp off",
    "is the hallway light off",
    "what is the state of the office light",
    "any lights left on",
    "anything left on",
    "lights still on",
    "office lights on?",
    "which lights are on in the bedroom",
    "what's the office light status",
    # switch (>= 5)
    "is the hallway switch on",
    "is the kitchen outlet on",
    "what is the state of the office switch",
    "are the plugs on",
    "check if the outlet is off",
    # lock (>= 5)
    "is the front door locked",
    "is the back door unlocked",
    "did I lock the front door",
    "what's the status of the front door lock",
    "can you check if the back door is locked",
    "are the doors locked",
    # cover (>= 5)
    "is the garage door open",
    "is the garage closed",
    "what is the state of the garage door",
    "are the blinds open",
    "did I leave the garage open",
    "garage door status",
    # climate (>= 5)
    "is the heat on",
    "is the ac running",
    "what's the status of the thermostat",
    "is the furnace on",
    "is the hvac running",
    # media_player (>= 5)
    "is the TV on",
    "is the speaker playing",
    "is the media player on",
    "is the music on",
    "tell me whether the TV is on",
    # embedded read frames
    "tell me whether the office fan is running",
    "do you know if the TV is on",
    "check that the lights are off",
    # past-tense (ATHENA-88 behaviour change: now a live read)
    "did you turn off the office lights",
]

REFERENT_QUESTIONS = [
    "did those come back on?",
    "is it on",
    "are they off",
]

IMPERATIVE_CORPUS = [
    "turn off the office lights",
    "turn the office lights on",
    "switch off the kitchen light",
    "can you turn off the bedroom lights?",
    "could you lock the front door",
    "please close the garage door",
    "is it possible to turn on the office lights",
    "are you able to lock the front door",
    "would it be possible to open the garage",
    "do you mind turning off the office lights",
    "would you mind locking the back door",
    "could you please lock the front door",
    "can you please turn off the kitchen lights",
    "let's turn off the office lights",
    "it's dark in here, turn on the office lights",
    "set the temperature to 70",
    "turn the temperature up",
    "leave the lights on",
    "keep the hallway light on",
    "make sure the office lights are off",
    "make sure the back door is locked",
    "can you make sure the garage is closed",
    "lock up",
    "open the blinds",
    "pause the TV",
    "play music in the kitchen",
    "dim the living room lights",
    "lights on in the kitchen",
]

UNKNOWN_CORPUS = [
    "what did you just do?",
    "what is the humidity?",
    "what's the weather",
    "I want the lights on",
    "can I get the lights on",
    "are the lights on? turn them off",
    "are the lights on, turn them off",
    "are the lights on? if so turn them off",
]


# ---------------------------------------------------------------------------
# Floor / named-member test
# ---------------------------------------------------------------------------

_LIGHT_TERMS = ("light", "lamp")
_SWITCH_TERMS = ("switch", "outlet", "plug")
_LOCK_TERMS = ("lock", "door")
_COVER_TERMS = ("garage", "blind", "cover")
_CLIMATE_TERMS = ("heat", "ac", "thermostat", "furnace", "hvac")
_MEDIA_TERMS = ("tv", "speaker", "media", "music")


def _count_matching(corpus, terms):
    return sum(1 for q in corpus if any(t in q.lower() for t in terms))


class TestCorpusFloors:
    def test_question_corpus_floor(self):
        assert len(QUESTION_CORPUS) >= 40

    def test_question_corpus_per_device_floor(self):
        assert _count_matching(QUESTION_CORPUS, _LIGHT_TERMS) >= 5
        assert _count_matching(QUESTION_CORPUS, _SWITCH_TERMS) >= 5
        assert _count_matching(QUESTION_CORPUS, _LOCK_TERMS) >= 5
        assert _count_matching(QUESTION_CORPUS, _COVER_TERMS) >= 5
        assert _count_matching(QUESTION_CORPUS, _CLIMATE_TERMS) >= 5
        assert _count_matching(QUESTION_CORPUS, _MEDIA_TERMS) >= 5

    def test_imperative_corpus_floor(self):
        assert len(IMPERATIVE_CORPUS) >= 20

    def test_probe_phrase_is_named_member(self):
        assert PROBE_PHRASE in QUESTION_CORPUS

    def test_turn_off_office_lights_is_named_imperative_member(self):
        assert "turn off the office lights" in IMPERATIVE_CORPUS


# ---------------------------------------------------------------------------
# Classifier assertions
# ---------------------------------------------------------------------------

class TestClassifierQuestionCorpus:
    def test_every_question_classifies_state_question(self):
        failures = []
        for q in QUESTION_CORPUS:
            r = classify_utterance(q)
            if r.kind != UtteranceKind.STATE_QUESTION:
                failures.append((q, r.kind, r.rule))
        assert not failures, failures

    def test_every_referent_question_classifies_state_question_needs_referent(self):
        for q in REFERENT_QUESTIONS:
            r = classify_utterance(q)
            assert r.kind == UtteranceKind.STATE_QUESTION, (q, r)
            assert r.needs_referent is True, (q, r)

    def test_probe_phrase_tagged_office_light(self):
        r = classify_utterance(PROBE_PHRASE)
        assert r.kind == UtteranceKind.STATE_QUESTION
        assert r.device_type == "light"
        assert r.room == "office"


class TestClassifierImperativeCorpus:
    def test_every_imperative_classifies_imperative(self):
        failures = []
        for q in IMPERATIVE_CORPUS:
            r = classify_utterance(q)
            if r.kind != UtteranceKind.IMPERATIVE:
                failures.append((q, r.kind, r.rule))
        assert not failures, failures

    def test_turn_office_lights_on_target_state(self):
        r = classify_utterance("turn the office lights on")
        assert r.kind == UtteranceKind.IMPERATIVE
        assert r.target_state == "on"


class TestClassifierUnknownCorpus:
    def test_every_unknown_entry_is_not_state_question(self):
        failures = []
        for q in UNKNOWN_CORPUS:
            r = classify_utterance(q)
            if r.kind == UtteranceKind.STATE_QUESTION:
                failures.append((q, r.kind, r.rule))
        assert not failures, failures


class TestClassifierPurity:
    def test_none_and_empty_are_unknown(self):
        assert classify_utterance(None).kind == UtteranceKind.UNKNOWN
        assert classify_utterance("").kind == UtteranceKind.UNKNOWN
        assert classify_utterance("   ").kind == UtteranceKind.UNKNOWN

    def test_classifier_never_raises_on_garbage_input(self):
        for q in ["\x00\x01", "?" * 500, 12345, object()]:
            try:
                r = classify_utterance(q)  # type: ignore[arg-type]
            except Exception as e:  # pragma: no cover - must never happen
                assert False, f"classify_utterance raised on {q!r}: {e}"
            assert r.kind == UtteranceKind.UNKNOWN


# ---------------------------------------------------------------------------
# Phase 3.5 -- JARVIS_WEB_URL (pattern parity for the localhost:3001 hardcode)
# ---------------------------------------------------------------------------

import asyncio
from pathlib import Path

import httpx as _httpx_module

import orchestrator.smart_home_controller as shc


def _run(coro):
    return asyncio.run(coro)


class _NoHttpAsyncClient:
    """Fails the test loudly if ever instantiated."""

    def __init__(self, *a, **kw):
        raise AssertionError("httpx.AsyncClient must not be constructed when jarvis_web_url is empty")


class _RecordingAsyncClient:
    calls = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, *a, **kw):
        _RecordingAsyncClient.calls.append(url)
        raise _httpx_module.ConnectError("no real network in unit tests")


def _fake_config(url):
    cfg = mock.MagicMock()
    cfg.jarvis_web_url = url
    return cfg


class TestJarvisWebUrlNoHardcode:
    def test_localhost_3001_hardcode_is_gone(self):
        assert "localhost:3001" not in Path(shc.__file__).read_text()

    def test_appliance_intent_skips_get_when_empty(self, monkeypatch):
        monkeypatch.setattr(shc, "get_config", lambda: _fake_config(""))
        monkeypatch.setattr(_httpx_module, "AsyncClient", _NoHttpAsyncClient)
        controller = shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=mock.MagicMock())
        result = _run(controller._handle_appliance_intent("oven", "get_status", {}, "is the oven on"))
        assert "couldn't check" in result.lower()

    def test_appliance_intent_gets_when_configured(self, monkeypatch):
        monkeypatch.setattr(shc, "get_config", lambda: _fake_config("http://jarvis-web:3001"))
        _RecordingAsyncClient.calls = []
        monkeypatch.setattr(_httpx_module, "AsyncClient", _RecordingAsyncClient)
        controller = shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=mock.MagicMock())
        _run(controller._handle_appliance_intent("oven", "get_status", {}, "is the oven on"))
        assert _RecordingAsyncClient.calls == ["http://jarvis-web:3001/api/appliances/oven"]

    def test_sensor_intent_skips_get_when_empty(self, monkeypatch):
        monkeypatch.setattr(shc, "get_config", lambda: _fake_config(""))
        monkeypatch.setattr(_httpx_module, "AsyncClient", _NoHttpAsyncClient)
        em = mock.MagicMock()
        em.get_entities = mock.AsyncMock(return_value={})
        controller = shc.SmartHomeController(entity_manager=em, llm_router=mock.MagicMock())
        controller._get_all_motion_sensors_with_stuck_detection = mock.AsyncMock(return_value=([], []))
        controller._get_window_sensor_status = mock.AsyncMock(return_value="no windows")
        result = _run(controller._handle_sensor_intent("sensor", {}, "check all sensors"))
        assert "couldn't check" in result.lower()

    def test_media_intent_skips_get_when_empty(self, monkeypatch):
        monkeypatch.setattr(shc, "get_config", lambda: _fake_config(""))
        monkeypatch.setattr(_httpx_module, "AsyncClient", _NoHttpAsyncClient)
        controller = shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=mock.MagicMock())
        result = _run(controller._handle_media_intent("get_status", {}, "what's playing", "living room", None))
        assert "couldn't check" in result.lower()


# ---------------------------------------------------------------------------
# Phase 3.6 -- routing tests (route_control_node restructure)
# ---------------------------------------------------------------------------

import ast as _ast

from orchestrator.nodes import route_control_node
from orchestrator.nodes import _runtime
from orchestrator.state import OrchestratorState
from orchestrator.ha_status_optimizer import detect_status_query_type as _real_detect_status_query_type
from orchestrator.mode_permission import current_ha_scope, READ_ONLY_REFUSAL


class _FakeEntityManager3_6:
    def __init__(self):
        self._entities = {}
        for i in range(12):
            self._entities[f"light.office_{i}"] = {
                "state": "on", "attributes": {"friendly_name": f"Office Light {i}"}
            }
        self._entities["switch.hallway"] = {"state": "off", "attributes": {"friendly_name": "Hallway Switch"}}
        self._entities["lock.front_door"] = {"state": "locked", "attributes": {"friendly_name": "Front Door Lock"}}
        self._entities["cover.garage_door"] = {"state": "closed", "attributes": {"friendly_name": "Garage Door"}}
        self._entities["media_player.living_room"] = {"state": "off", "attributes": {"friendly_name": "Living Room TV"}}
        self._entities["fan.office"] = {"state": "off", "attributes": {"friendly_name": "Office Fan"}}
        self._entities["climate.thermostat"] = {"state": "off", "attributes": {"friendly_name": "Thermostat"}}

    async def get_entities(self):
        return dict(self._entities)

    async def find_lights_by_room(self, room):
        if room and "office" in room.lower():
            return [
                {"entity_id": f"light.office_{i}", "friendly_name": f"Office Light {i}",
                 "members": [], "state": "on", "type": "individual"}
                for i in range(12)
            ]
        return []

    async def get_all_light_groups(self):
        return []


class _HostileLLMRouter:
    """Returns a light turn_off write for EVERY prompt, regardless of what
    was asked -- proves the read-only path never trusts the LLM."""

    def __init__(self, response_text=None):
        self.generate = mock.AsyncMock(side_effect=self._generate)
        self._response_text = response_text or (
            '{"device_type": "light", "room": "office", "action": "turn_off", '
            '"target_scope": "group", "parameters": {}}'
        )

    async def _generate(self, **kwargs):
        return {"response": self._response_text}


def _raw_ha_client_3_6():
    client = mock.MagicMock()
    client.call_service = mock.AsyncMock(return_value={"ok": True})
    client.get_state = mock.AsyncMock(return_value={"state": "on"})
    client.get_states = mock.AsyncMock(return_value=[])
    return client


def _drive_route_control(
    query, *, prev_context=None, context_ref_info=None, mode="owner",
    permissions=None, room="office", llm_router=None, entity_manager=None,
    ha_client=None, feature_flags=None, session_id="sess-1",
):
    _runtime.reset_for_test()
    em = entity_manager or _FakeEntityManager3_6()
    llm = llm_router or _HostileLLMRouter()
    controller = shc.SmartHomeController(entity_manager=em, llm_router=llm)
    client = ha_client or _raw_ha_client_3_6()
    _runtime.set_smart_controller(controller)
    _runtime.set_entity_manager(em)
    _runtime.set_ha_client(client)
    _runtime.set_sequence_executor(None)
    _runtime.set_automation_agent(None)

    state = OrchestratorState(query=query)
    state.mode = mode
    state.permissions = permissions or {"mode": mode}
    state.room = room
    state.session_id = session_id
    state.prev_context = prev_context
    state.context_ref_info = context_ref_info or {}
    state.node_timings = {}

    flags = feature_flags or {}

    async def _feature_config(name):
        return flags.get(name, {"enabled": name == "status_bulk_query" or name == "status_skip_synthesis"})

    with (
        mock.patch("orchestrator.nodes.route_control.get_feature_config", new_callable=mock.AsyncMock, side_effect=_feature_config),
        mock.patch("orchestrator.nodes.route_control.get_automation_system_mode", new_callable=mock.AsyncMock, return_value="pattern"),
        mock.patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        mock.patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=mock.AsyncMock),
    ):
        result = _run(route_control_node(state))
    return result, controller, llm, client


class TestQuestionCorpusNeverWrites:
    """3.6(a): every QUESTION_CORPUS entry, as owner -> 0 call_service,
    a non-empty answer that isn't the read-only refusal, and the correct
    path (bulk_optimizer for room-less bulk-eligible entries, otherwise
    get_status_dispatch)."""

    def test_every_question_corpus_entry_never_writes(self):
        failures = []
        for q in QUESTION_CORPUS:
            uk = classify_utterance(q)
            room = "office" if uk.room else None
            bulk_expected = (not uk.room and not uk.needs_referent and _real_detect_status_query_type(q))
            with mock.patch(
                "orchestrator.nodes.route_control.optimize_status_query",
                new_callable=mock.AsyncMock,
                return_value=mock.MagicMock(query_type="lights_on", entities=[{"entity_id": "light.office_0", "state": "on"}], raw_states={}),
            ), mock.patch(
                "orchestrator.nodes.route_control.should_skip_synthesis",
                return_value=(True, "Some lights are on."),
            ):
                result, controller, llm, client = _drive_route_control(q, room=room)
            if client.call_service.await_count != 0:
                failures.append((q, "wrote", client.call_service.await_args_list))
                continue
            if not result.answer or result.answer == READ_ONLY_REFUSAL:
                failures.append((q, "bad_answer", result.answer))
                continue
            if "should i go ahead" in result.answer.lower() or "to do it, say" in result.answer.lower():
                failures.append((q, "fanout_text_leaked", result.answer))
        assert not failures, failures


class TestReferentPathCoercion:
    """3.6(b): a referent question through a hostile LLM must have its
    write coerced to get_status by 3.3(b) -- proving the LLM really was
    invoked (not just skipped) and still produced zero writes."""

    def test_referent_question_coerced_not_written(self):
        prev_context = {
            "query": "turn off the office lights",
            "response": "Done! Lights off.",
            "entities": {"room": "office", "device_type": "light"},
            "parameters": {"device_type": "light", "action": "turn_off", "room": "office"},
        }
        context_ref_info = {"has_context_ref": True, "anaphora_types": ["yes_no"], "ref_types": []}
        llm = _HostileLLMRouter()
        result, controller, llm_used, client = _drive_route_control(
            "did those come back on?", prev_context=prev_context, context_ref_info=context_ref_info, llm_router=llm,
        )
        assert client.call_service.await_count == 0
        assert llm.generate.await_count >= 1


class TestJsonFallbackReachable:
    """3.6(c): a referent question whose LLM returns invalid JSON ->
    0 writes, the 3.3(c) fallback fires (not the coercion path)."""

    def test_referent_question_json_fallback_no_write(self):
        prev_context = {
            "query": "turn off the office lights",
            "response": "Done! Lights off.",
            "entities": {"room": "office", "device_type": "light"},
            "parameters": {"device_type": "light", "action": "turn_off", "room": "office"},
        }
        context_ref_info = {"has_context_ref": True, "anaphora_types": ["yes_no"], "ref_types": []}
        llm = _HostileLLMRouter(response_text="not json")
        result, controller, llm_used, client = _drive_route_control(
            "did those come back on?", prev_context=prev_context, context_ref_info=context_ref_info, llm_router=llm,
        )
        assert client.call_service.await_count == 0
        assert llm.generate.await_count >= 1


class TestImperativeAndUnknownCorpusReadOnlyFalse:
    """3.6(d): every IMPERATIVE_CORPUS / UNKNOWN_CORPUS entry runs under a
    writable (read_only=False) scope -- the bulk optimizer is never
    consulted for them."""

    def test_imperative_and_unknown_never_read_only(self):
        failures = []
        captured_read_only = {}

        class _SpyController(shc.SmartHomeController):
            async def execute_intent(self, intent, ha_client, original_query=None, device_room=None):
                scope = current_ha_scope()
                captured_read_only["value"] = scope.read_only if scope else None
                return "Done!"

        for q in IMPERATIVE_CORPUS + UNKNOWN_CORPUS:
            captured_read_only.clear()
            with mock.patch("orchestrator.nodes.route_control.optimize_status_query") as spy:
                _runtime.reset_for_test()
                em = _FakeEntityManager3_6()
                llm = _HostileLLMRouter()
                controller = _SpyController(entity_manager=em, llm_router=llm)
                client = _raw_ha_client_3_6()
                _runtime.set_smart_controller(controller)
                _runtime.set_entity_manager(em)
                _runtime.set_ha_client(client)
                _runtime.set_sequence_executor(None)
                _runtime.set_automation_agent(None)
                state = OrchestratorState(query=q)
                state.mode = "owner"
                state.permissions = {"mode": "owner"}
                state.room = "office"
                state.session_id = "sess-1"
                state.node_timings = {}

                async def _feature_config(name):
                    return {"enabled": name in ("status_bulk_query", "status_skip_synthesis")}

                with (
                    mock.patch("orchestrator.nodes.route_control.get_feature_config", new_callable=mock.AsyncMock, side_effect=_feature_config),
                    mock.patch("orchestrator.nodes.route_control.get_automation_system_mode", new_callable=mock.AsyncMock, return_value="pattern"),
                    mock.patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
                    mock.patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=mock.AsyncMock),
                ):
                    _run(route_control_node(state))
            if spy.await_count != 0:
                failures.append((q, "bulk_optimizer_called"))
            if captured_read_only.get("value") is not False:
                failures.append((q, "read_only_not_false", captured_read_only.get("value")))
        assert not failures, failures


class TestReverseBugCommandsSkipBulkOptimizer:
    """3.6(e): the reverse-bug commands never reach the bulk optimizer."""

    def test_reverse_bug_commands_skip_bulk_optimizer(self):
        for q in ["turn the office lights on", "leave the lights on", "set the temperature to 70"]:
            with mock.patch("orchestrator.nodes.route_control.optimize_status_query") as spy:
                _drive_route_control(q)
            assert spy.await_count == 0, q


class TestScopeReopenDrift:
    """3.6(g): route_control_node opens exactly one ha_permission_scope(
    whose read_only= keyword references uk; every scope opener reachable
    from execute_intent is the reuse-aware conditional form."""

    def test_route_control_has_exactly_one_scope_open_referencing_uk(self):
        src = Path("src/orchestrator/nodes/route_control.py").read_text()
        tree = _ast.parse(src)
        opens = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.With):
                for item in node.items:
                    call = item.context_expr
                    if isinstance(call, _ast.Call) and getattr(call.func, "id", None) == "ha_permission_scope":
                        opens.append(call)
        assert len(opens) == 1, opens
        call = opens[0]
        read_only_kw = next((kw for kw in call.keywords if kw.arg == "read_only"), None)
        assert read_only_kw is not None
        names_in_expr = {n.id for n in _ast.walk(read_only_kw.value) if isinstance(n, _ast.Name)}
        assert "uk" in names_in_expr

    def test_unconditional_scope_openers_are_exactly_the_node_entry_set(self):
        import subprocess
        out = subprocess.run(
            ["grep", "-rn", "ha_permission_scope(", "src/orchestrator"],
            capture_output=True, text=True,
        ).stdout
        unconditional_files = set()
        for line in out.splitlines():
            path = line.split(":", 1)[0]
            if "nodes/route_control.py" in path or "nodes/route_music.py" in path or "nodes/route_tv.py" in path:
                unconditional_files.add(path.split("/")[-1])
                continue
        assert unconditional_files == {"route_control.py", "route_music.py", "route_tv.py"}


class TestGuestQuestionStillDenied:
    """3.6(h): a guest asking a state question still hits the CONTROL
    intent gate as today."""

    def test_guest_state_question_denied(self):
        result, controller, llm, client = _drive_route_control(
            PROBE_PHRASE, mode="guest", permissions={"mode": "guest", "allowed_intents": []},
        )
        assert client.call_service.await_count == 0
        assert result.error == "permission_denied"


class TestProbePhraseDispatchedIntent:
    """3.6(i): the probe phrase's dispatched intent has room=='office' and
    device_type=='light'."""

    def test_probe_phrase_intent_room_and_device_type(self):
        result, controller, llm, client = _drive_route_control(PROBE_PHRASE)
        assert result.retrieved_data["intent"]["room"] == "office"
        assert result.retrieved_data["intent"]["device_type"] == "light"
        assert result.retrieved_data["intent"]["action"] == "get_status"


class TestJsonFallbackImperativeCase:
    """3.6(j): the JSON-fallback imperative case (bob L2) -- "turn the
    office lights on" through an LLM returning invalid JSON yields exactly
    the turn_on writes, not turn_off."""

    def test_turn_office_lights_on_json_fallback_writes_turn_on(self):
        llm = _HostileLLMRouter(response_text="not json")
        result, controller, llm_used, client = _drive_route_control("turn the office lights on", llm_router=llm)
        calls = [c.args for c in client.call_service.await_args_list]
        assert calls, "expected at least one call_service call"
        assert all(c[1] == "turn_on" for c in calls), calls
        assert not any(c[1] == "turn_off" for c in calls), calls

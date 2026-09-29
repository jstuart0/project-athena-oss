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
    "did I lock the back door",
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
    # past-tense lock/cover checks name the device by their verb
    "did you unlock the front door",
    "did you close the garage door",
    "did you close the front door",
    # a room that is also a device noun survives before another device noun
    "are the garage lights on",
    "is the garage fan on",
    "are the media room lights on",
    "are the tvs on",
]

REFERENT_QUESTIONS = [
    "did those come back on?",
    "is it on",
    "are they off",
    "is it locked",
    "are they unlocked",
    # Named member: a referent question that also matches a status
    # pattern ("status of"), so it proves referents never reach the bulk
    # optimizer.
    "what's the status of those",
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
    "is it ok to turn off the lights",
    "do you think you could lock the door",
    "can i get you to close the blinds",
    "turn off the garage lights",
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
# Classifier hardening: bounded cost, word-boundary vocabulary, real rooms,
# configurable assistant name
# ---------------------------------------------------------------------------

import time as _time

# Crafted to hit every former backtracking shape (`.*` chains in the wh /
# how-many / ensure-state / door noun-map patterns). ~4 KB each.
_CRAFTED_SLOW_INPUTS = [
    "are the lights " + "what is " * 500,
    "which " + "is are " * 680,
    "how many " + "are is " * 680,
    "make sure " + "is are " * 680,
    "the door " * 450 + "locked",
    "the doors " * 400 + "opened",
]

_CLASSIFY_BUDGET_SECONDS = 0.05


class TestClassifierBoundedCost:
    def test_crafted_inputs_are_at_least_4kb(self):
        assert all(len(s) >= 3200 for s in _CRAFTED_SLOW_INPUTS)
        assert max(len(s) for s in _CRAFTED_SLOW_INPUTS) >= 4000

    def test_crafted_inputs_classify_within_budget(self):
        slow = []
        for s in _CRAFTED_SLOW_INPUTS:
            t0 = _time.perf_counter()
            classify_utterance(s)
            elapsed = _time.perf_counter() - t0
            if elapsed >= _CLASSIFY_BUDGET_SECONDS:
                slow.append((s[:30], round(elapsed, 3)))
        assert not slow, slow

    def test_huge_input_classifies_within_budget(self):
        """The prefix bound: even with every gap bounded, a 100 KB input
        would cost seconds if classified whole."""
        huge = "are the lights " + "what is " * 12_500
        assert len(huge) >= 100_000
        t0 = _time.perf_counter()
        classify_utterance(huge)
        assert _time.perf_counter() - t0 < _CLASSIFY_BUDGET_SECONDS

    def test_every_pattern_is_bounded_without_the_prefix_cap(self):
        """Each classifier pattern is cheap on its own, so the prefix cap
        isn't the only defence (a future caller or pattern can't bring the
        cubic cost back)."""
        from orchestrator import utterance_kind as ukm

        patterns = [
            ukm._WH_STATE_RE, ukm._ENSURE_STATE_RE, ukm._NOUN_FIRST_STATUS_RE, ukm._EMBEDDED_READ_RE,
            ukm._STATE_WORD_RE, ukm._DEVICE_NOUN_RE, ukm._BARE_COMMAND_RE, ukm._PAST_ACTION_CHECK_RE,
            *ukm._ELLIPTIC_PATTERNS, *ukm._ROOM_FRAME_RES, *(p for p, _ in ukm._NOUN_MAP),
        ]
        slow = []
        for s in _CRAFTED_SLOW_INPUTS:
            for pattern in patterns:
                t0 = _time.perf_counter()
                list(pattern.finditer(s.lower()))
                elapsed = _time.perf_counter() - t0
                if elapsed >= _CLASSIFY_BUDGET_SECONDS:
                    slow.append((pattern.pattern[:40], s[:20], round(elapsed, 3)))
        assert not slow, slow

    def test_long_input_still_classifies_its_prefix(self):
        """The bound keeps a real question at the front of an over-long
        utterance a question -- truncation isn't a blanket UNKNOWN."""
        r = classify_utterance(PROBE_PHRASE + " " + "blah " * 200)
        assert r.kind == UtteranceKind.STATE_QUESTION
        assert r.room == "office"


class TestStateWordsAreWholeWords:
    def test_phone_number_is_not_a_state_question(self):
        assert classify_utterance("what is the phone number").kind != UtteranceKind.STATE_QUESTION

    def test_phone_charged_is_not_a_state_question(self):
        assert classify_utterance("is the phone charged?").kind != UtteranceKind.STATE_QUESTION

    def test_word_ending_in_on_is_not_an_ensure_state_imperative(self):
        """`on` inside a word no longer satisfies the make-sure frame."""
        assert classify_utterance("can you make sure dinner is salmon").kind != UtteranceKind.IMPERATIVE

    def test_whole_word_state_still_matches(self):
        r = classify_utterance("what is on in the kitchen")
        assert r.kind == UtteranceKind.STATE_QUESTION


# Every corpus entry's expected room (None = no room named). The last five
# are the librarian's non-room captures.
EXPECTED_ROOMS = {
    PROBE_PHRASE: "office",
    "are the kitchen lights on": "kitchen",
    "is the bedroom lamp off": "bedroom",
    "is the hallway light off": "hallway",
    "what is the state of the office light": "office",
    "any lights left on": None,
    "anything left on": None,
    "lights still on": None,
    "office lights on?": "office",
    "which lights are on in the bedroom": "bedroom",
    "what's the office light status": "office",
    "is the hallway switch on": "hallway",
    "is the kitchen outlet on": "kitchen",
    "what is the state of the office switch": "office",
    "are the plugs on": None,
    "check if the outlet is off": None,
    "is the front door locked": None,
    "is the back door unlocked": None,
    "did I lock the front door": None,
    "did I lock the back door": None,
    "what's the status of the front door lock": None,
    "can you check if the back door is locked": None,
    "are the doors locked": None,
    "is the garage door open": None,
    "is the garage closed": None,
    "what is the state of the garage door": None,
    "are the blinds open": None,
    "did I leave the garage open": None,
    "garage door status": None,
    "is the heat on": None,
    "is the ac running": None,
    "what's the status of the thermostat": None,
    "is the furnace on": None,
    "is the hvac running": None,
    "is the TV on": None,
    "is the speaker playing": None,
    "is the media player on": None,
    "is the music on": None,
    "tell me whether the TV is on": None,
    "tell me whether the office fan is running": "office",
    "do you know if the TV is on": None,
    "check that the lights are off": None,
    "did you turn off the office lights": "office",
    "did those come back on?": None,
    "is it on": None,
    "are they off": None,
    "is it locked": None,
    "are they unlocked": None,
    "what's the status of those": None,
    "turn off the office lights": "office",
    "turn the office lights on": "office",
    "switch off the kitchen light": "kitchen",
    "can you turn off the bedroom lights?": "bedroom",
    "could you lock the front door": None,
    "please close the garage door": None,
    "is it possible to turn on the office lights": "office",
    "are you able to lock the front door": None,
    "would it be possible to open the garage": None,
    "do you mind turning off the office lights": "office",
    "would you mind locking the back door": None,
    "could you please lock the front door": None,
    "can you please turn off the kitchen lights": "kitchen",
    "let's turn off the office lights": "office",
    "it's dark in here, turn on the office lights": "office",
    "set the temperature to 70": None,
    "turn the temperature up": None,
    "leave the lights on": None,
    "keep the hallway light on": "hallway",
    "make sure the office lights are off": "office",
    "make sure the back door is locked": None,
    "can you make sure the garage is closed": None,
    "lock up": None,
    "open the blinds": None,
    "pause the TV": None,
    "play music in the kitchen": "kitchen",
    "dim the living room lights": "living room",
    "lights on in the kitchen": "kitchen",
    "are all the lights off?": None,
    "what lights are on": None,
    "are any kitchen lights on": "kitchen",
    "is my office light on": "office",
    "which office lights are on": "office",
    "what kitchen lights are on": "kitchen",
    "turn off the lights in the living room": "living room",
    "did you unlock the front door": None,
    "did you close the garage door": None,
    "did you close the front door": None,
    "are the garage lights on": "garage",
    "is the garage fan on": "garage",
    "are the media room lights on": "media room",
    "are the tvs on": None,
    "is it ok to turn off the lights": None,
    "do you think you could lock the door": None,
    "can i get you to close the blinds": None,
    "turn off the garage lights": "garage",
}


# Device type for the entries whose type isn't carried by an obvious noun.
EXPECTED_DEVICE_TYPES = {
    "did you unlock the front door": "lock",
    "did I lock the front door": "lock",
    "did you close the garage door": "cover",
    "did you close the front door": "cover",
    "are the garage lights on": "light",
    "is the garage fan on": "fan",
    "are the media room lights on": "light",
    "are the tvs on": "media_player",
    "is the garage door open": "cover",
    "what's the status of the front door lock": "lock",
    "is the heater on": None,
}


class TestClassifierDeviceTypes:
    def test_device_type_values(self):
        wrong = []
        for q, expected in EXPECTED_DEVICE_TYPES.items():
            actual = classify_utterance(q).device_type
            if actual != expected:
                wrong.append((q, expected, actual))
        assert not wrong, wrong


class TestClassifierRooms:
    def test_every_corpus_entry_has_an_expected_room(self):
        missing = [q for q in QUESTION_CORPUS + REFERENT_QUESTIONS + IMPERATIVE_CORPUS if q not in EXPECTED_ROOMS]
        assert not missing, missing

    def test_room_values(self):
        wrong = []
        for q, expected in EXPECTED_ROOMS.items():
            actual = classify_utterance(q).room
            if actual != expected:
                wrong.append((q, expected, actual))
        assert not wrong, wrong

    def test_all_the_lights_question_stays_bulk_eligible(self):
        r = classify_utterance("are all the lights off?")
        assert r.kind == UtteranceKind.STATE_QUESTION
        assert r.room is None and not r.needs_referent


class TestConfigurableAssistantName:
    def test_default_names_are_stripped(self):
        for name in ("jarvis", "Athena"):
            r = classify_utterance(f"{name}, are the office lights on")
            assert r.kind == UtteranceKind.STATE_QUESTION, name
            assert r.room == "office", name

    def test_configured_name_is_stripped(self):
        r = classify_utterance("Friday, are the office lights on", assistant_names=("Friday",))
        assert r.kind == UtteranceKind.STATE_QUESTION
        assert r.room == "office"

    def test_unconfigured_name_is_not_a_filler(self):
        """Positive control: without the configured name, the vocative
        blocks the question frame -- so the test above proves the name
        was used."""
        r = classify_utterance("Friday, are the office lights on")
        assert r.kind != UtteranceKind.STATE_QUESTION

    def test_name_with_regex_metacharacters_is_literal(self):
        r = classify_utterance("a.i., are the office lights on", assistant_names=("a.i.",))
        assert r.kind == UtteranceKind.STATE_QUESTION
        assert classify_utterance("axib are the office lights on", assistant_names=("a.i.",)).kind != UtteranceKind.STATE_QUESTION


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
from contextlib import nullcontext as _nullcontext

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

    async def get_climate_state(self):
        return {"entity_id": "climate.thermostat", "state": "heat", "current_temp": 68,
                "target_temp": 70, "hvac_action": "heating"}


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
    ha_client=None, feature_flags=None, session_id="sess-1", fanout_limits=None,
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

    from orchestrator import write_fanout as _wf
    limits_cfg = None
    if fanout_limits is not None:
        limits_cfg = mock.MagicMock(
            ha_write_fanout_confirm_threshold=fanout_limits[0], ha_write_fanout_hard_limit=fanout_limits[1],
        )
    with (
        mock.patch("orchestrator.nodes.route_control.get_feature_config", new_callable=mock.AsyncMock, side_effect=_feature_config),
        mock.patch("orchestrator.nodes.route_control.get_automation_system_mode", new_callable=mock.AsyncMock, return_value="pattern"),
        mock.patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        mock.patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=mock.AsyncMock),
        (mock.patch.object(_wf, "get_config", lambda: limits_cfg) if limits_cfg is not None else _nullcontext()),
    ):
        result = _run(route_control_node(state))
    return result, controller, llm, client


_ANSWER_TERMS = {
    "light": ("light",),
    "switch": ("switch",),
    "lock": ("lock",),
    "cover": ("garage", "cover", "blind"),
    "climate": ("thermostat",),
    "media_player": ("tv", "media"),
    "fan": ("fan",),
}


def _question_path_run(q, room):
    """One question through route_control with spies on the three layers a
    zero-write result could come from: the bulk optimizer, the get_status
    dispatch (execute_intent's action and the scope's read_only at call
    time), and the fan-out gate."""
    from orchestrator import write_fanout as _wf

    seen = []
    real_execute = shc.SmartHomeController.execute_intent

    async def _execute_spy(self, intent, *a, **kw):
        scope = current_ha_scope()
        seen.append((intent.get("action"), scope.read_only if scope else None))
        return await real_execute(self, intent, *a, **kw)

    optimize = mock.AsyncMock(return_value=mock.MagicMock(
        query_type="lights_on", entities=[{"entity_id": "light.office_0", "state": "on"}], raw_states={},
    ))
    gate_spy = mock.MagicMock(side_effect=_wf.gate)
    gate_many_spy = mock.MagicMock(side_effect=_wf.gate_many)
    with (
        mock.patch("orchestrator.nodes.route_control.optimize_status_query", optimize),
        mock.patch("orchestrator.nodes.route_control.should_skip_synthesis", return_value=(True, "Some lights are on.")),
        mock.patch.object(shc.SmartHomeController, "execute_intent", _execute_spy),
        mock.patch.object(_wf, "gate", gate_spy),
        mock.patch.object(_wf, "gate_many", gate_many_spy),
    ):
        result, controller, llm, client = _drive_route_control(q, room=room)
    return result, client, optimize, seen, gate_spy.call_count + gate_many_spy.call_count


class TestQuestionCorpusNeverWrites:
    """3.6(a): every QUESTION_CORPUS entry, as owner, is answered by the
    read path -- not merely with zero writes, which the fan-out gate could
    also produce. Per entry: the bulk optimizer exactly when bulk-eligible
    (room-less, non-referent, a status-pattern match), otherwise one
    get_status dispatch under a read-only scope; the gate is never
    reached; and the answer names the asked device's domain."""

    def test_every_question_corpus_entry_takes_the_read_path(self):
        failures = []
        for q in QUESTION_CORPUS:
            uk = classify_utterance(q)
            room = "office" if uk.room else None
            bulk_expected = bool(not uk.room and not uk.needs_referent and _real_detect_status_query_type(q))
            result, client, optimize, seen, gate_calls = _question_path_run(q, room)
            answer = (result.answer or "").lower()
            if client.call_service.await_count != 0:
                failures.append((q, "wrote", client.call_service.await_args_list))
            if gate_calls:
                failures.append((q, "gate_reached", gate_calls))
            if not answer or result.answer == READ_ONLY_REFUSAL or result.error:
                failures.append((q, "bad_answer", result.answer, result.error))
            if "should i go ahead" in answer or "to do it, say" in answer:
                failures.append((q, "fanout_text_leaked", result.answer))
            if bulk_expected:
                if optimize.await_count != 1 or seen:
                    failures.append((q, "expected_bulk", optimize.await_count, seen))
            else:
                if optimize.await_count != 0 or seen != [("get_status", True)]:
                    failures.append((q, "expected_read_only_get_status", optimize.await_count, seen))
                if not any(term in answer for term in _ANSWER_TERMS[uk.device_type or "light"]):
                    failures.append((q, "answer_not_about_device", uk.device_type, result.answer))
        assert not failures, failures

    def test_a_referent_question_matches_a_status_pattern(self):
        """Population check for the test below: without a referent that the
        bulk optimizer's patterns would accept, 'referents never reach the
        optimizer' can't be observed."""
        assert _real_detect_status_query_type("what's the status of those")
        assert classify_utterance("what's the status of those").needs_referent

    def test_referent_questions_without_context_read_under_read_only(self):
        """No context to resolve against: the referent question reaches
        extraction (never the bulk optimizer or the dispatch) and still
        executes only a get_status, read-only."""
        failures = []
        for q in REFERENT_QUESTIONS:
            result, client, optimize, seen, gate_calls = _question_path_run(q, "office")
            if client.call_service.await_count or gate_calls or optimize.await_count:
                failures.append((q, client.call_service.await_count, gate_calls, optimize.await_count))
            if seen != [("get_status", True)]:
                failures.append((q, "seen", seen))
        assert not failures, failures


_PREV_OFFICE_OFF = {
    "query": "turn off the office lights",
    "response": "Done! Lights off.",
    "entities": {"room": "office", "device_type": "light"},
    "parameters": {"device_type": "light", "action": "turn_off", "room": "office"},
}
_CONTINUED_PRONOUN = {"has_context_ref": True, "anaphora_types": ["pronoun"], "ref_types": ["pronoun"]}
_NO_DEVICE_TYPE_TURN_OFF = '{"room": "office", "action": "turn_off", "target_scope": "group", "parameters": {}}'


def _drive_referent(llm_text=None):
    """"did those come back on?" after an office turn_off, with the fan-out
    limits off (t = h = 0) so the gate can't be what stops a write."""
    metric = mock.MagicMock()
    llm = _HostileLLMRouter(response_text=llm_text)
    with (
        mock.patch("orchestrator.metrics.state_question_routed_total", metric),
        mock.patch("orchestrator.nodes.route_control.state_question_routed_total", metric),
    ):
        result, controller, llm_used, client = _drive_route_control(
            "did those come back on?", prev_context=dict(_PREV_OFFICE_OFF),
            context_ref_info=dict(_CONTINUED_PRONOUN), llm_router=llm, fanout_limits=(0, 0),
        )
    paths = [c.kwargs.get("path") for c in metric.labels.call_args_list]
    return result, llm, client, paths


def _assert_answered_by_a_read(result, client):
    assert client.call_service.await_count == 0, client.call_service.await_args_list
    assert result.error is None, result.error
    assert result.answer and result.answer != READ_ONLY_REFUSAL
    assert "to do it, say" not in result.answer.lower()
    assert "should i go ahead" not in result.answer.lower()


class TestReferentPathCoercion:
    """3.6(b): a referent question through a hostile LLM is coerced to
    get_status by 3.3(b) and answered -- with the gate disabled, so zero
    writes can only come from the question layer."""

    def test_referent_question_coerced_and_answered(self):
        result, llm, client, paths = _drive_referent()
        _assert_answered_by_a_read(result, client)
        assert llm.generate.await_count >= 1
        assert paths.count("llm_coerced") == 1, paths

    def test_referent_llm_intent_without_device_type_is_still_a_read(self):
        """The LLM intent lacks device_type, so the continuation merge is
        skipped and the previous write action survives into the intent --
        only the post-merge get_status force keeps it a read."""
        result, llm, client, paths = _drive_referent(_NO_DEVICE_TYPE_TURN_OFF)
        _assert_answered_by_a_read(result, client)
        assert llm.generate.await_count >= 1


class TestJsonFallbackReachable:
    """3.6(c): a referent question whose LLM returns invalid JSON takes the
    3.3(c) fallback (not the coercion path) and is answered."""

    def test_referent_question_json_fallback_answered(self):
        result, llm, client, paths = _drive_referent("not json")
        _assert_answered_by_a_read(result, client)
        assert llm.generate.await_count >= 1
        assert paths.count("json_fallback") == 1, paths
        assert "llm_coerced" not in paths


def _extract(query, utterance, llm_text):
    llm = _HostileLLMRouter(response_text=llm_text)
    controller = shc.SmartHomeController(entity_manager=_FakeEntityManager3_6(), llm_router=llm)
    with mock.patch.object(shc, "get_admin_client") as admin:
        admin.return_value.get_component_model = mock.AsyncMock(return_value=None)
        intent = _run(controller.extract_intent(query, utterance=utterance))
    assert llm.generate.await_count == 1, "the LLM path must actually be exercised"
    return intent


class TestExtractIntentQuestionGuards:
    """3.3(b)/(c) directly on extract_intent, independent of route_control's
    post-merge force and the read-only scope."""

    Q = "did those come back on?"

    def test_hostile_json_is_coerced_to_get_status(self):
        assert _extract(self.Q, classify_utterance(self.Q), None)["action"] == "get_status"

    def test_hostile_json_without_device_type_is_coerced(self):
        assert _extract(self.Q, classify_utterance(self.Q), _NO_DEVICE_TYPE_TURN_OFF)["action"] == "get_status"

    def test_invalid_json_falls_back_to_get_status(self):
        intent = _extract(self.Q, classify_utterance(self.Q), "not json")
        assert intent["action"] == "get_status"

    def test_invalid_json_for_an_imperative_uses_its_target_state(self):
        """3.6(j)'s route-level case never reaches the fallback ("turn the
        office lights on" is resolved before the LLM), so the IMPERATIVE
        fallback is pinned here."""
        from orchestrator.utterance_kind import UtteranceClassification
        for target, expected in (("on", "turn_on"), ("off", "turn_off")):
            uk = UtteranceClassification(kind=UtteranceKind.IMPERATIVE, device_type="light", target_state=target)
            assert _extract("do the thing in the den", uk, "not json")["action"] == expected, target


class _EntityStateManager:
    def __init__(self, entities):
        self._entities = entities

    async def get_entities(self):
        return dict(self._entities)


def _entity_state_answer(entities, domain, room):
    controller = shc.SmartHomeController(entity_manager=_EntityStateManager(entities), llm_router=mock.MagicMock())
    return _run(controller._handle_entity_state_query(domain, room, "is it on"))


def _switch(name, state):
    return {"state": state, "attributes": {"friendly_name": name}}


class TestEntityStateQuery:
    """3.4: the generic read handler for switch / media_player state."""

    MIXED = {
        "switch.office_desk": _switch("Office Desk Switch", "on"),
        "switch.office_fan_plug": _switch("Office Fan Plug", "on"),
        "switch.office_heater": _switch("Office Heater", "off"),
        "switch.kitchen_kettle": _switch("Kitchen Kettle", "on"),
    }

    def test_single_entity_in_the_room(self):
        entities = {"switch.office_desk": _switch("Office Desk Switch", "on"),
                    "switch.kitchen_kettle": _switch("Kitchen Kettle", "off")}
        assert _entity_state_answer(entities, "switch", "office") == "The Office Desk Switch is on."

    def test_n_of_m_on_in_the_room(self):
        answer = _entity_state_answer(self.MIXED, "switch", "office")
        assert answer == "2 of 3 switches are on: Office Desk Switch and Office Fan Plug."

    def test_none_on_in_the_room(self):
        entities = {"switch.office_a": _switch("Office A", "off"), "switch.office_b": _switch("Office B", "off"),
                    "switch.kitchen_kettle": _switch("Kitchen Kettle", "on")}
        assert _entity_state_answer(entities, "switch", "office") == "No switches are currently on in the office."

    def test_room_not_found(self):
        assert _entity_state_answer(self.MIXED, "switch", "garage") == "I couldn't find a switch in the garage."

    def test_media_player_noun(self):
        entities = {"media_player.office_tv": _switch("Office TV", "playing"),
                    "media_player.office_speaker": _switch("Office Speaker", "idle")}
        assert _entity_state_answer(entities, "media_player", "office") == "1 of 2 media players are on: Office TV."


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


def _is_reuse_aware_test(test):
    """`current_ha_scope() is None`."""
    return (
        isinstance(test, _ast.Compare)
        and isinstance(test.left, _ast.Call)
        and getattr(test.left.func, "id", getattr(test.left.func, "attr", None)) == "current_ha_scope"
        and len(test.ops) == 1 and isinstance(test.ops[0], _ast.Is)
        and isinstance(test.comparators[0], _ast.Constant) and test.comparators[0].value is None
    )


def _classify_scope_openers(tree):
    """[(Call, conditional)] for every ha_permission_scope(...) call. A call
    is conditional only as the body of `... if current_ha_scope() is None
    else ...` (the reuse-aware form)."""
    conditional_ids = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.IfExp) and _is_reuse_aware_test(node.test) and isinstance(node.body, _ast.Call):
            conditional_ids.add(id(node.body))
    found = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "ha_permission_scope":
            found.append((node, id(node) in conditional_ids))
    return sorted(found, key=lambda t: t[0].lineno)


def _scan_scope_openers():
    results = []
    for path in sorted(Path("src/orchestrator").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "ha_permission_scope(" not in text:
            continue
        for call, conditional in _classify_scope_openers(_ast.parse(text)):
            results.append((str(path), call, conditional))
    return results


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

    NODE_ENTRY_OPENERS = {
        "src/orchestrator/nodes/route_control.py",
        "src/orchestrator/nodes/route_music.py",
        "src/orchestrator/nodes/route_tv.py",
    }

    def test_unconditional_scope_openers_are_exactly_the_node_entry_set(self):
        openers = _scan_scope_openers()
        unconditional = {path for path, call, conditional in openers if not conditional}
        assert unconditional == self.NODE_ENTRY_OPENERS
        outside_nodes = [(p, c) for p, c, _ in openers if "/nodes/" not in p]
        assert all(cond for p, _, cond in openers if "/nodes/" not in p), outside_nodes
        conditional_files = {p for p, _, cond in openers if cond}
        assert len([1 for _, _, cond in openers if cond]) >= 3
        assert "src/orchestrator/smart_home_controller.py" in conditional_files

    def test_every_node_entry_opener_passes_read_only_from_the_classifier(self):
        for path, call, conditional in _scan_scope_openers():
            if conditional:
                continue
            read_only_kw = next((kw for kw in call.keywords if kw.arg == "read_only"), None)
            assert read_only_kw is not None, path
            names = {n.id for n in _ast.walk(read_only_kw.value) if isinstance(n, _ast.Name)}
            assert "uk" in names, (path, _ast.unparse(read_only_kw.value))

    def test_scanner_flags_an_unconditional_opener_outside_nodes(self):
        src = (
            "import contextlib\n"
            "def f():\n"
            "    with ha_permission_scope(None, mode='system'):\n"
            "        pass\n"
            "def g():\n"
            "    cm = ha_permission_scope(None, mode='system') if current_ha_scope() is None else contextlib.nullcontext()\n"
            "    with cm:\n"
            "        pass\n"
        )
        found = _classify_scope_openers(_ast.parse(src))
        assert [cond for _, cond in found] == [False, True]

    def test_scanner_rejects_a_conditional_on_the_wrong_test(self):
        src = (
            "import contextlib\n"
            "def f(x):\n"
            "    cm = ha_permission_scope(None) if x else contextlib.nullcontext()\n"
        )
        assert [cond for _, cond in _classify_scope_openers(_ast.parse(src))] == [False]


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


def test_probe_phrase_never_writes():
    """Verification (3): the incident phrase, as owner, through an LLM that
    answers every prompt with a turn_off write -> zero call_service calls,
    and a state answer rather than a refusal or a fan-out prompt."""
    result, controller, llm, client = _drive_route_control(PROBE_PHRASE)
    assert client.call_service.await_count == 0, client.call_service.await_args_list
    assert result.answer and result.answer != READ_ONLY_REFUSAL
    assert "should i go ahead" not in result.answer.lower()
    assert "to do it, say" not in result.answer.lower()


def test_probe_phrase_harness_records_writes_when_routing_and_gate_bypassed():
    """Positive control for the test above: with the question routing
    bypassed (classifier forced to UNKNOWN) and the fan-out limits off, the
    same harness records the incident's office-light turn_off writes -- so
    zero writes above is the fix, not a harness that can't see writes."""
    from orchestrator import write_fanout
    from orchestrator.utterance_kind import UNKNOWN_CLASSIFICATION

    cfg = mock.MagicMock(ha_write_fanout_confirm_threshold=0, ha_write_fanout_hard_limit=0)
    with mock.patch("orchestrator.nodes.route_control.classify_utterance", return_value=UNKNOWN_CLASSIFICATION), \
            mock.patch.object(write_fanout, "get_config", lambda: cfg):
        result, controller, llm, client = _drive_route_control(PROBE_PHRASE)
    services = [c.args[1] for c in client.call_service.await_args_list]
    assert len(services) >= 11, services
    assert set(services) == {"turn_off"}


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


class TestConfiguredAssistantNameRouting:
    """The assistant profile's configured name is stripped as a leading
    vocative before classification (OSS-first: not just the shipped
    names)."""

    @staticmethod
    def _reset_cache():
        from orchestrator.nodes import route_control as rc
        rc._assistant_names_cache.update(names=(), expires_at=0.0)

    def test_configured_name_question_takes_the_read_path(self, monkeypatch):
        from orchestrator.nodes import route_control as rc

        async def _names():
            return ("Friday",)

        monkeypatch.setattr(rc, "_configured_assistant_names", _names)
        result, controller, llm, client = _drive_route_control("Friday, are the office lights on")
        assert client.call_service.await_count == 0
        assert result.retrieved_data["intent"]["action"] == "get_status"
        assert result.retrieved_data["intent"]["room"] == "office"

    def test_lookup_reads_the_profile_once_per_ttl(self, monkeypatch):
        from orchestrator.nodes import route_control as rc
        self._reset_cache()
        calls = []

        async def _profile():
            calls.append(1)
            return {"assistant_name": "Friday"}

        monkeypatch.setattr(rc, "get_assistant_profile", _profile)
        try:
            assert _run(rc._configured_assistant_names()) == ("Friday",)
            assert _run(rc._configured_assistant_names()) == ("Friday",)
            assert len(calls) == 1
        finally:
            self._reset_cache()

    def test_slow_profile_lookup_is_bounded(self, monkeypatch):
        from orchestrator.nodes import route_control as rc
        self._reset_cache()

        async def _slow_profile():
            await asyncio.sleep(5)
            return {"assistant_name": "Friday"}

        monkeypatch.setattr(rc, "get_assistant_profile", _slow_profile)
        monkeypatch.setattr(rc, "ASSISTANT_NAME_LOOKUP_TIMEOUT_SECONDS", 0.01)
        try:
            t0 = _time.perf_counter()
            assert _run(rc._configured_assistant_names()) == ()
            assert _time.perf_counter() - t0 < 1.0
        finally:
            self._reset_cache()


# ---------------------------------------------------------------------------
# Music and TV nodes: a state question runs under a read-only scope
# ---------------------------------------------------------------------------

import orchestrator.tv_handler as _tv_handler_module
from orchestrator.nodes import route_music_node, route_tv_node
from orchestrator.state import IntentCategory

_TV_CONFIGS = {"living room": {"media_player_entity_id": "media_player.living_room", "display_name": "living room"}}


def _node_state(query, intent):
    state = OrchestratorState(query=query)
    state.intent = intent
    state.mode = "owner"
    state.permissions = {"mode": "owner"}
    state.room = "living room"
    state.session_id = "sess-node"
    state.node_timings = {}
    state.retrieved_data = {}
    return state


def _drive_tv(query, *, kill_switch=False):
    _runtime.reset_for_test()
    raw = _raw_ha_client_3_6()
    _runtime.set_tv_handler(_tv_handler_module.AppleTVHandler(raw, mock.MagicMock()))
    with (
        mock.patch.object(_tv_handler_module, "get_tv_configs", new_callable=mock.AsyncMock, return_value=_TV_CONFIGS),
        mock.patch("orchestrator.nodes.route_tv.get_feature_config", new_callable=mock.AsyncMock,
                   return_value={"enabled": kill_switch}),
        mock.patch("orchestrator.nodes.route_tv.store_conversation_context", new_callable=mock.AsyncMock),
    ):
        out = _run(route_tv_node(_node_state(query, IntentCategory.TV_CONTROL)))
    return out, raw


class TestTvNodeQuestionsAreReadOnly:
    def test_past_tense_question_never_powers_the_tv(self):
        """"did you turn off the tv" parses to a power-off; the read-only
        scope denies it before the HA client."""
        assert classify_utterance("did you turn off the tv").kind == UtteranceKind.STATE_QUESTION
        out, raw = _drive_tv("did you turn off the tv")
        assert raw.call_service.await_count == 0, raw.call_service.await_args_list
        assert out.answer == READ_ONLY_REFUSAL

    def test_command_still_powers_the_tv(self):
        """Positive control: the same harness records the write for a
        command."""
        out, raw = _drive_tv("turn off the tv")
        assert [c.args[:2] for c in raw.call_service.await_args_list] == [("media_player", "turn_off")]

    def test_kill_switch_reverts_the_read_only_scope(self):
        out, raw = _drive_tv("did you turn off the tv", kill_switch=True)
        assert raw.call_service.await_count == 1


class _ScopeRecordingMusicHandler:
    def __init__(self):
        self.read_only_seen = []

    async def parse_music_control_intent(self, query, room=None):
        return {"action": "now_playing" if "playing" in query else "pause", "room": room}

    async def handle_control(self, action, room=None, volume_level=None):
        self.read_only_seen.append(current_ha_scope().read_only)
        return "Nothing is playing in the living room right now."


def _drive_music(query, *, kill_switch=False):
    _runtime.reset_for_test()
    handler = _ScopeRecordingMusicHandler()
    _runtime.set_music_handler(handler)
    with (
        mock.patch("orchestrator.nodes.route_music.get_feature_config", new_callable=mock.AsyncMock,
                   return_value={"enabled": kill_switch}),
        mock.patch("orchestrator.nodes.route_music.store_conversation_context", new_callable=mock.AsyncMock),
    ):
        _run(route_music_node(_node_state(query, IntentCategory.MUSIC_CONTROL)))
    return handler


class TestMusicNodeQuestionsAreReadOnly:
    def test_is_music_playing_runs_read_only(self):
        assert _drive_music("is music playing").read_only_seen == [True]

    def test_music_command_runs_writable(self):
        assert _drive_music("pause the music").read_only_seen == [False]

    def test_kill_switch_reverts_the_read_only_scope(self):
        assert _drive_music("is music playing", kill_switch=True).read_only_seen == [False]


# ---------------------------------------------------------------------------
# Metric labels come from the classifier, never from LLM text
# ---------------------------------------------------------------------------

class TestCoercionMetricLabelIsClosed:
    def test_llm_device_type_never_becomes_a_label(self):
        hostile = _HostileLLMRouter(response_text=(
            '{"device_type": "attacker-chosen-label", "room": "office", "action": "turn_off", '
            '"target_scope": "group", "parameters": {}}'
        ))
        controller = shc.SmartHomeController(entity_manager=_FakeEntityManager3_6(), llm_router=hostile)
        metric = mock.MagicMock()
        with mock.patch("orchestrator.metrics.state_question_routed_total", metric), \
                mock.patch.object(shc, "get_admin_client") as admin:
            admin.return_value.get_component_model = mock.AsyncMock(return_value=None)
            for q, expected in (("did those come back on?", "unknown"), ("is the office lamp on?", "light")):
                metric.reset_mock()
                intent = _run(controller.extract_intent(q, utterance=classify_utterance(q)))
                assert intent["action"] == "get_status"
                metric.labels.assert_called_once_with(device_type=expected, path="llm_coerced")

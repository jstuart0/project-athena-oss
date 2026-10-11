"""extract_intent returns the room the utterance names and nothing else.

The speaker's room is never filled in here (execute_intent applies it), a
model's room must be named in the utterance, and the whole house is chosen
only by whole-house wording. Fictional houses only.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import room_context as rc  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from shared.ha_registry import Area, room_key  # noqa: E402


def area(name, aliases=()):
    return Area(f"area_{room_key(name)}", name, tuple(aliases), room_key(name))


HOUSE_A = rc.RoomDirectory.build(areas=[
    area("North Bathroom"), area("South Bathroom"), area("Lower Bathroom"),
    area("Study", ["Den"]), area("Galley"), area("Alpha Room"), area("Living Room (2)"), area("On"),
])
HOUSE_B = rc.RoomDirectory.build(areas=[area("Bathroom"), area("Master Bathroom"), area("Study")])


class _Provider:
    def __init__(self, directory):
        self._directory = directory

    def current(self):
        return self._directory


@pytest.fixture(autouse=True)
def _clean_runtime():
    _runtime.reset_for_test()
    yield
    _runtime.reset_for_test()


def use(directory):
    _runtime.set_room_directory(_Provider(directory))


class _Admin:
    async def get_component_model(self, name):
        return None


class _Llm:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def generate(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        return {"response": self.reply}


def controller(llm=None):
    return shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=llm)


def extract(query, device_room=None, ctl=None):
    return asyncio.run((ctl or controller()).extract_intent(query, device_room=device_room))


# ---------------------------------------------------------------------------
# What the utterance names
# ---------------------------------------------------------------------------

def test_the_south_bathroom_is_extracted_as_its_key():
    use(HOUSE_A)
    assert extract("turn on the south bathroom lights")["room"] == "south_bathroom"


def test_master_bathroom_is_not_read_as_bathroom_where_both_exist():
    use(HOUSE_B)
    assert extract("turn on the master bathroom lights")["room"] == "master_bathroom"
    assert extract("turn on the bathroom lights")["room"] == "bathroom"


def test_master_bathroom_is_extracted_whole_even_with_no_directory():
    assert extract("turn on the master bathroom lights")["room"] == "master_bathroom"


def test_an_ambiguous_partial_name_is_extracted_as_said_for_the_decision_to_resolve():
    use(HOUSE_A)
    assert extract("turn on the bathroom lights")["room"] == "bathroom"


def test_a_plural_room_phrase_extracts_every_room():
    use(HOUSE_A)
    got = extract("turn off all the bathrooms")
    assert got["room"] == "multi_room" and sorted(got["rooms"]) == ["lower_bathroom", "north_bathroom", "south_bathroom"]


def test_rooms_joined_by_and_extract_as_several():
    use(HOUSE_A)
    got = extract("turn on the study and the galley lights")
    assert got["room"] == "multi_room" and got["rooms"] == ["study", "galley"]


def test_a_room_less_command_names_no_room_even_with_a_device_room():
    use(HOUSE_A)
    got = extract("turn on the lights", device_room="study")
    assert got["room"] is None, got


def test_a_creative_colour_without_a_room_does_not_become_the_whole_house():
    use(HOUSE_A)
    assert extract("make it sunset", device_room="study")["room"] is None


def test_alpha_is_a_room_only_where_the_house_has_it():
    use(HOUSE_A)
    assert extract("turn on the alpha room lights")["room"] == "alpha_room"
    use(HOUSE_B)
    assert extract("turn on the alpha room lights")["room"] is None


def test_a_numbered_area_is_found_by_its_plain_name():
    use(HOUSE_A)
    assert extract("turn on the living room lights")["room"] == "living_room_2"


# ---------------------------------------------------------------------------
# Whole-house wording
# ---------------------------------------------------------------------------

def test_all_the_lights_in_a_named_room_acts_on_that_room_only():
    use(HOUSE_A)
    got = extract("turn off all the lights in the galley")
    assert got["room"] == "galley" and got["action"] == "turn_off", got


def test_all_the_lights_with_no_room_is_the_whole_house():
    use(HOUSE_A)
    assert extract("turn off all the lights", device_room="study")["room"] == "whole_house"


def test_everything_in_a_named_room_is_that_room():
    use(HOUSE_A)
    assert extract("turn off everything in the study")["room"] == "study"


def test_an_exclusion_is_resolved_through_the_directory():
    use(HOUSE_A)
    got = extract("turn off all the lights except the galley")
    assert got["room"] == "whole_house" and got["excluded_rooms"] == ["galley"], got


def test_an_ambiguous_exclusion_excludes_every_match():
    use(HOUSE_A)
    got = extract("turn off all lights except the bathroom")
    assert sorted(got["excluded_rooms"]) == ["lower_bathroom", "north_bathroom", "south_bathroom"]


def test_leave_one_on_and_turn_off_the_rest_excludes_that_room():
    use(HOUSE_A)
    got = extract("leave the galley on but turn off the rest")
    assert got["room"] == "whole_house" and got["excluded_rooms"] == ["galley"], got


def test_colour_commands_use_the_same_whole_house_rule():
    use(HOUSE_A)
    assert extract("make all the lights blue", device_room="study")["room"] == "whole_house"
    assert extract("make all the lights in the galley blue", device_room="study")["room"] == "galley"


# ---------------------------------------------------------------------------
# Every fast-path family: a named room is kept, the device room is not added
# ---------------------------------------------------------------------------

FAMILIES = [
    ("turn-off phrase", "kill the lights in the galley", "kill the lights", "turn_off"),
    ("turn-on phrase", "hit the lights in the galley", "hit the lights", "turn_on"),
    ("brighter", "make the galley lights brighter", "make it brighter", "increase"),
    ("dimmer", "the galley is too bright", "it is too bright", "decrease"),
    ("simple on", "turn on the galley lights", "turn on the lights", "turn_on"),
    ("simple off", "turn off the galley lights", "turn off the lights", "turn_off"),
    ("colour", "make the galley lights blue", "make the lights blue", "set_color"),
    ("colour temperature", "make the galley lights warmer", "make the lights warmer", "set_color"),
    ("ambient", "make the galley lights sunset", "make it sunset", "set_color"),
    ("brightness percent", "dim the galley lights to 50%", "dim the lights to 50%", "set_brightness"),
]
IDS = [f[0] for f in FAMILIES]


@pytest.mark.parametrize("label,named,roomless,action", FAMILIES, ids=IDS)
def test_fast_path_family_keeps_the_room_said(label, named, roomless, action):
    use(HOUSE_A)
    got = extract(named, device_room="study")
    assert got["room"] == "galley" and got["action"] == action, (label, named, got)


@pytest.mark.parametrize("label,named,roomless,action", FAMILIES, ids=IDS)
def test_fast_path_family_never_adds_the_device_room(label, named, roomless, action):
    use(HOUSE_A)
    got = extract(roomless, device_room="study")
    assert got["room"] is None and got["action"] == action, (label, roomless, got)


# ---------------------------------------------------------------------------
# The language model's room
# ---------------------------------------------------------------------------

def llm_intent(room, query, excluded=None):
    body = {"device_type": "light", "room": room, "action": "turn_on", "target_scope": "group",
            "parameters": {}, "color_description": None}
    if excluded is not None:
        body["excluded_rooms"] = excluded
    import json

    ctl = controller(_Llm(json.dumps(body)))
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        return asyncio.run(ctl.extract_intent(query))


def test_a_model_whole_house_for_a_query_without_whole_house_wording_is_dropped():
    use(HOUSE_A)
    assert llm_intent("whole_house", "please make the place cosy")["room"] is None


def test_a_model_whole_house_for_whole_house_wording_is_kept():
    use(HOUSE_A)
    assert llm_intent("whole_house", "get everything glowing in a calm way")["room"] == "whole_house"


def test_a_model_room_that_is_inside_another_word_is_ungrounded():
    use(HOUSE_A)
    assert llm_intent("den", "do the thing to the garden lights")["room"] is None


def test_a_model_room_the_utterance_does_not_name_is_ungrounded():
    use(HOUSE_A)
    assert llm_intent("galley", "make it cosy in here")["room"] is None


def test_a_model_room_the_utterance_names_is_kept_as_a_key():
    use(HOUSE_A)
    assert llm_intent("galley", "get the galley cosy")["room"] == "galley"
    assert llm_intent("living room", "get the living room cosy")["room"] == "living_room_2"


def test_a_model_exclusion_must_be_named_too():
    use(HOUSE_A)
    got = llm_intent("whole_house", "put everything on except the galley", excluded=["galley", "study"])
    assert got["room"] == "whole_house" and got["excluded_rooms"] == ["galley"], got


def test_the_prompt_does_not_tell_the_model_where_the_speaker_is():
    use(HOUSE_A)
    llm = _Llm('{"device_type": "light", "room": null, "action": "turn_on", "target_scope": "group", "parameters": {}}')
    ctl = controller(llm)
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        asyncio.run(ctl.extract_intent("do the thing", device_room="study"))
    assert "speaking from" not in llm.prompts[0] and "Device location" not in llm.prompts[0]


# ---------------------------------------------------------------------------
# The other two places that used to keep their own room lists
# ---------------------------------------------------------------------------

def _classify(query, monkeypatch):
    import os
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    if str(SRC / "orchestrator") not in sys.path:
        sys.path.insert(0, str(SRC / "orchestrator"))  # classify_node imports semantic_cache bare
    os.environ.setdefault("SERVICE_API_KEY", "test-key-room-mentions")
    os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
    import shared.config as config_module

    loader = MagicMock()
    loader.get_config = config_module.get_config
    loader.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
    loader.get_feature_flag = AsyncMock(return_value=False)
    loader.get_feature_flags = AsyncMock(return_value={})
    loader.clear_cache = AsyncMock()
    sys.modules.setdefault("orchestrator.config_loader", loader)
    import orchestrator.nodes  # noqa: F401
    import orchestrator.main as main_module
    from orchestrator.search_providers.intent_classifier import IntentClassifier
    from orchestrator.state import OrchestratorState

    cache = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock(), client=None)
    _runtime.set_cache_client(cache)
    _runtime.set_llm_router(MagicMock())
    _runtime.set_intent_classifier(IntentClassifier())
    state = OrchestratorState(query=query)
    state.session_id = "sess-room-mentions"
    state.mode = "owner"
    state.node_timings = {}
    return asyncio.run(main_module.classify_node(state))


def test_classify_reads_a_room_and_a_colour_as_light_control_with_the_room_key(monkeypatch):
    use(HOUSE_A)
    from orchestrator.state import IntentCategory

    state = _classify("change the galley to white", monkeypatch)
    assert state.intent == IntentCategory.CONTROL
    assert state.entities.get("room") == "galley" and state.entities.get("color") == "white", state.entities


def test_classify_does_not_treat_a_house_room_as_a_room_when_the_house_lacks_it(monkeypatch):
    use(HOUSE_B)
    state = _classify("change the galley to white", monkeypatch)
    assert state.entities.get("room") != "galley"


def test_the_multi_intent_splitter_keeps_rooms_joined_by_and_together():
    from orchestrator.search_providers.intent_classifier import IntentClassifier

    use(HOUSE_A)
    query = "turn on the alpha room and the galley lights"
    assert IntentClassifier().detect_multi_intent(query) == [query]
    other = "what is the weather and turn on the lights"
    assert len(IntentClassifier().detect_multi_intent(other)) == 2

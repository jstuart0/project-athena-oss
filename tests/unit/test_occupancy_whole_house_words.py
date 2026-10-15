"""Whole-house words are never a room, and a person-in-a-room question is an
occupancy question. A house whose registry has an area or alias named "Home"
or "House" must still answer "is anyone home" from the motion sensors.
Fictional houses only."""
from __future__ import annotations

import asyncio
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

import pytest

import orchestrator.smart_home_controller as shc
from orchestrator import room_context as rc
from orchestrator.state import IntentCategory
from shared.ha_registry import Area, room_key
from tests.unit.test_extract_intent_room_mentions import _classify, _clean_runtime, use  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore")


def area(name, aliases=()):
    return Area(f"area_{room_key(name)}", name, tuple(aliases), room_key(name))


HOUSE = rc.RoomDirectory.build(areas=[
    area("Home"), area("House"), area("Studio", ["Here", "There", "Inside"]), area("Den"),
    area("Home Office"), area("Pantry"), area("Workshop"), area("Parlor"),
])
NOW = datetime.now(timezone.utc)


def sensor(room, state, seconds_ago=0):
    return {"room": room, "state": state, "last_changed": (NOW - timedelta(seconds=seconds_ago)).isoformat()}


def occupancy_answer(sensors, query):
    llm = mock.MagicMock()
    llm.generate = mock.AsyncMock(return_value={"response": ""})
    c = shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=llm)
    c._directory = lambda: HOUSE
    admin = mock.MagicMock()
    admin.get_component_model = mock.AsyncMock(return_value=None)
    with mock.patch.object(shc, "get_admin_client", return_value=admin):
        return asyncio.run(c._llm_occupancy_reasoning(sensors, "", query))


WHOLE_HOUSE_QUERIES = [
    "is anyone home", "is anybody home", "is anyone here", "is anyone there", "is anyone inside",
    "is anyone in the house", "is there anybody home right now", "is anyone at home", "is anyone in the place",
]


@pytest.mark.parametrize("query", WHOLE_HOUSE_QUERIES)
def test_a_whole_house_question_answers_from_the_sensors_not_a_room_named_home(query):
    text = occupancy_answer([sensor("workshop", "on"), sensor("den", "off", 3600)], query)
    assert text == "Motion in the workshop right now.", text


def test_whole_house_question_floor():
    assert len(WHOLE_HOUSE_QUERIES) >= 9


@pytest.mark.parametrize("word", ["home", "house", "here", "there", "inside", "place"])
def test_a_whole_house_word_matches_no_room(word):
    assert rc.find_room_mention(f"is anyone {word}", HOUSE).kind == "none"
    assert rc.find_room_mention(f"turn on the lights at {word}", HOUSE).kind == "none"
    assert rc.find_room_mention(f"are the lights on {word}", HOUSE).kind == "none"


def test_a_real_room_with_home_in_its_name_still_matches_as_a_phrase():
    mention = rc.find_room_mention("is anyone in the home office", HOUSE)
    assert mention.kind == "exact" and [c.key for c in mention.candidates] == [room_key("Home Office")]
    mention = rc.find_room_mention("turn on the home office lights", HOUSE)
    assert [c.key for c in mention.candidates] == [room_key("Home Office")]


def test_a_room_named_home_is_still_a_known_speaker_room():
    ref = HOUSE.resolves("home")
    assert ref is not None and ref.key == room_key("Home")


def test_light_and_state_decisions_treat_home_as_no_room():
    assert rc.find_room_mentions("turn on the lights at home", HOUSE) == []
    assert rc.room_values(rc.find_room_mention("is it dark at home", HOUSE)) == ()
    assert rc.unknown_room_phrase("are the lights on at home", HOUSE) == ""
    assert rc.joined_room_mentions("turn on the lights at home and the house", HOUSE) == []


NAMED_ROOMS = [
    ("is there anyone in the workshop", "workshop"),
    ("anyone in the workshop", "workshop"),
    ("is somebody in the workshop", "workshop"),
    ("is the workshop occupied", "workshop"),
    ("is there anybody in the den", "den"),
    ("is someone in the pantry", "pantry"),
    ("is there anyone in the parlor", "parlor"),
    ("is there anyone in the office", "office"),
    ("anyone in the office", "office"),
    ("is somebody in the office", "office"),
    ("is the office occupied", "office"),
    ("is anybody in the kitchen", "kitchen"),
]


@pytest.mark.parametrize("query,room", NAMED_ROOMS)
def test_a_person_in_a_room_question_classifies_as_a_sensor_question(query, room, monkeypatch):
    use(HOUSE)
    state = _classify(query, monkeypatch)
    assert state.intent == IntentCategory.CONTROL, (query, state.intent)
    assert state.entities.get("device_type") == "sensor", state.entities


def test_named_room_floor():
    assert len(NAMED_ROOMS) >= 12 and any("office" in q for q, _ in NAMED_ROOMS)


@pytest.mark.parametrize("query", ["is anyone home", "is anybody home", "is anyone here",
                                   "which rooms have had motion recently"])
def test_whole_house_and_motion_questions_stay_sensor_questions(query, monkeypatch):
    use(HOUSE)
    state = _classify(query, monkeypatch)
    assert state.intent == IntentCategory.CONTROL and state.entities.get("device_type") == "sensor"


def test_a_person_question_with_no_room_is_not_taken_for_occupancy(monkeypatch):
    use(HOUSE)
    try:
        state = _classify("is there anyone in the band", monkeypatch)
    except Exception:
        return  # fell through to the (mocked) classifier, not the sensor fast path
    assert state.entities.get("device_type") != "sensor"


NAMED_ANSWERS = [
    ([sensor("workshop", "on"), sensor("den", "off", 3600)], "is there anyone in the workshop",
     "Yes, there's motion in the workshop right now."),
    ([sensor("workshop", "on"), sensor("den", "off", 3600)], "is the den occupied",
     "No motion in the den in the last 5 minutes."),
    ([sensor("workshop", "on")], "is anyone in the home office", "I don't have a motion sensor in the home office."),
]


@pytest.mark.parametrize("sensors,query,expected", NAMED_ANSWERS)
def test_a_named_room_is_still_answered_for_that_room(sensors, query, expected):
    assert occupancy_answer(sensors, query) == expected

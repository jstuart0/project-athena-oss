"""Occupancy answers (ATHENA-269): a plain template, no markdown, the room
the person named, and a model only for a head-count over several rooms."""
from __future__ import annotations

import asyncio
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

import pytest

import orchestrator.smart_home_controller as shc
from orchestrator import room_context
from shared.ha_registry import Area

pytestmark = pytest.mark.filterwarnings("ignore")

DIRECTORY = room_context.RoomDirectory.build(areas=[
    Area(f"area_{k}", n, (), k) for k, n in (("office", "Office"), ("den", "Den"), ("kitchen", "Kitchen"))
])
NOW = datetime.now(timezone.utc)


def sensor(room, state, seconds_ago=0):
    return {"room": room, "state": state, "last_changed": (NOW - timedelta(seconds=seconds_ago)).isoformat()}


def controller(llm_text=None):
    llm = mock.MagicMock()
    llm.generate = mock.AsyncMock(return_value={"response": llm_text or ""})
    c = shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=llm)
    c._directory = lambda: DIRECTORY
    return c, llm


def answer(sensors, query, llm_text=None):
    c, llm = controller(llm_text)
    admin = mock.MagicMock()
    admin.get_component_model = mock.AsyncMock(return_value=None)
    with mock.patch.object(shc, "get_admin_client", return_value=admin):
        return asyncio.run(c._llm_occupancy_reasoning(sensors, "", query)), llm


TEMPLATES = [
    ([sensor("office", "on")], "is anyone home", "Motion in the office right now."),
    ([sensor("office", "on"), sensor("den", "on")], "is anyone home", "Motion in the den and the office right now."),
    ([sensor("office", "off", 60)], "is anyone home",
     "No motion right now; there was some in the office in the last 5 minutes."),
    ([sensor("office", "off", 3600)], "is anyone home",
     "No motion anywhere in the last 5 minutes; someone may still be home."),
]


@pytest.mark.parametrize("sensors,query,expected", TEMPLATES)
def test_template_table(sensors, query, expected):
    text, llm = answer(sensors, query)
    assert text == expected
    assert llm.generate.await_count == 0
    assert not any(ch in text for ch in "*#`\n") and "confidence" not in text.lower()


def test_template_table_floor():
    assert len(TEMPLATES) >= 4


NAMED = [
    ([sensor("office", "on"), sensor("den", "off", 3600)], "is there anyone in the office",
     "Yes, there's motion in the office right now."),
    ([sensor("office", "on"), sensor("den", "off", 3600)], "is anyone in the den",
     "No motion in the den in the last 5 minutes."),
    ([sensor("den", "off", 30)], "is there anybody in the den",
     "No motion in the den right now, but there was some in the last 5 minutes."),
    ([sensor("office", "on")], "is there anyone in the kitchen", "I don't have a motion sensor in the kitchen."),
]


@pytest.mark.parametrize("sensors,query,expected", NAMED)
def test_a_named_room_is_answered_for_that_room(sensors, query, expected):
    text, llm = answer(sensors, query)
    assert text == expected and llm.generate.await_count == 0


def test_an_unknown_room_is_named_back():
    c, _ = controller()
    c._get_all_motion_sensors_with_stuck_detection = mock.AsyncMock(return_value=([sensor("office", "on")], []))
    text = asyncio.run(c._estimate_occupancy("is there anyone in the pantry"))
    assert text == "I don't know a room called pantry."
    c._get_all_motion_sensors_with_stuck_detection.assert_not_awaited()


@pytest.mark.parametrize("query", ["is anyone in the house", "is there anybody home right now", "is anyone here"])
def test_whole_house_words_are_not_unknown_rooms(query):
    c, _ = controller()
    assert c._occupancy_unknown_room(query) is None


def test_the_model_is_only_asked_a_head_count_over_several_rooms():
    sensors = [sensor("office", "on"), sensor("den", "on")]
    text, llm = answer(sensors, "are there at least two people home", "Two people are likely home.")
    assert llm.generate.await_count == 1 and text == "Two people are likely home."
    _, llm = answer([sensor("office", "on")], "are there at least two people home")
    assert llm.generate.await_count == 0


@pytest.mark.parametrize("bad", ["1.", "**2 people** are home", "- one\n- two", "1. Office\n2. Den", ""])
def test_a_markdown_or_list_model_reply_falls_back_to_the_template(bad):
    sensors = [sensor("office", "on"), sensor("den", "on")]
    text, _ = answer(sensors, "how many people are home", bad)
    assert text == "Motion in the den and the office right now."


def test_the_controller_falls_back_to_the_configured_default_model():
    """Its four fallbacks read athena_default_model; the shared drift test
    takes this file in once the orchestrator joins its scan."""
    import ast
    from pathlib import Path

    path = Path(shc.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and "llama3.1:8b" in n.value]
    assert literals == []
    assert path.read_text(encoding="utf-8").count("else get_config().athena_default_model") == 4

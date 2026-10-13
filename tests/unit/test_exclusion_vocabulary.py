"""One exclusion vocabulary: "apart from", "besides" and their peers exclude
rooms everywhere an exclusion is read.

room_context.EXCLUSION_PHRASES is the only list. The room parser, the two
whole-house fast-path guards, the music handler's exclusion regex and the
intent prompt all derive from it or name every phrase in it; a drift scan fails
when a second list of the words appears in the orchestrator. Fictional houses
only.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import room_context as rc  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from shared.ha_registry import Area  # noqa: E402

from .test_room_light_area_tier import GALLEY_LEAVES, Rig  # noqa: E402
from .test_room_light_area_tier import _isolated  # noqa: E402,F401  (the autouse runtime/config reset)

ROOT = Path(__file__).resolve().parents[2]
GALLEY = rc.RoomRef("galley", "galley")

HOUSE = rc.RoomDirectory.build(areas=[
    Area("area_galley", "Galley", (), "galley"),
    Area("area_study", "Study", (), "study"),
    Area("area_hall", "Hall", (), "hall"),
])

# Every phrase the plan names plus the two inflections that read as exclusions.
PHRASINGS = [
    "except", "except for", "excepting", "but not", "but", "other than", "excluding",
    "apart from", "aside from", "besides", "not including", "not the",
]


def test_the_vocabulary_holds_every_phrasing_the_matrix_uses():
    spoken = {" ".join(p) for p in rc.EXCLUSION_PHRASES}
    for phrasing in PHRASINGS:
        assert phrasing in spoken or phrasing.split()[0] in spoken, phrasing


# ---------------------------------------------------------------------------
# The room parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrasing", PHRASINGS)
def test_every_phrasing_excludes_the_named_room(phrasing):
    query = f"turn off all the lights {phrasing} the galley"
    assert rc.has_exclusion(query), query
    mention = rc.find_room_mention(query, HOUSE)
    decision = rc.decide_room(mention, query=query, speaker_room="study", directory=HOUSE)
    assert decision == rc.WholeHouse((GALLEY,)), (query, decision)
    assert rc.explicit_whole_house(query, HOUSE) == rc.WholeHouse((GALLEY,)), query


def test_apart_from_and_besides_lead_with_the_exclusion_not_a_room():
    for phrasing in ("apart from", "aside from", "besides", "not including", "excepting"):
        toks = f"turn off every light {phrasing} the galley".split()
        assert rc._scan_exclusions(toks, [])[0] == toks.index(phrasing.split()[0]), phrasing


@pytest.mark.parametrize("query", [
    "press the button", "ask the butler to turn on the lights", "what is this about",
    "turn off all the lights", "butter the toast",
])
def test_a_word_that_merely_contains_but_is_not_an_exclusion(query):
    assert rc.has_exclusion(query) is False, query


@pytest.mark.parametrize("phrasing", ["apart from", "aside from", "besides", "excluding", "except"])
def test_an_unknown_room_phrase_stops_at_the_exclusion_word(phrasing):
    query = f"turn off the lights in the sunroom {phrasing} the galley"
    assert rc.unknown_room_phrase(query, HOUSE) == "sunroom", phrasing


# ---------------------------------------------------------------------------
# The two whole-house fast-path guards
# ---------------------------------------------------------------------------

def _controller():
    _runtime.set_room_directory(type("P", (), {"current": lambda self: HOUSE})())
    return shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=None)


class _Admin:
    async def get_component_model(self, name):
        return None


class _CountingLlm:
    """Answers with the whole house minus the galley and counts its calls."""

    def __init__(self, reply=None):
        self.calls = 0
        self.reply = reply or json.dumps({
            "device_type": "light", "room": "whole_house", "excluded_rooms": ["galley"],
            "action": "turn_off", "target_scope": "group", "parameters": {}, "color_description": None})

    async def generate(self, **kwargs):
        self.calls += 1
        return {"response": self.reply}


def _extract(query, llm=None, directory=HOUSE):
    _runtime.set_room_directory(type("P", (), {"current": lambda self: directory})())
    llm = llm or _CountingLlm()
    ctl = shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=llm)
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        return asyncio.run(ctl.extract_intent(query)), llm


@pytest.mark.parametrize("phrasing", PHRASINGS)
@pytest.mark.parametrize("opening", ["turn off all the lights", "kill all the lights", "flip on all the lights"])
def test_every_fast_path_family_reads_every_phrasing_as_an_exclusion(opening, phrasing):
    got, llm = _extract(f"{opening} {phrasing} the galley")
    assert got.get("room") == "whole_house" and got.get("excluded_rooms") == ["galley"], (opening, phrasing, got)


@pytest.mark.parametrize("query", [
    "press the button and turn off all the lights", "the butler said turn off all the lights",
    "what about turning everything off", "turn off all the lights",
])
def test_the_guard_does_not_defer_a_word_that_contains_but(query):
    assert _controller()._defers_to_model(query) is False, query


# Wordings outside the phrase list: they defer to the model, and the server
# never answers them by writing the excluded room.
OTHER_WORDINGS = [
    "turn off all the lights without the galley", "turn off all the lights unless the galley",
    "turn off all the lights minus the galley", "turn off all the lights and skip the galley",
    "turn off all the lights, leave the galley on", "turn off all the lights and keep the galley on",
    "turn off all the lights exclude the galley", "turn off all the lights with the galley excluded",
    "turn off all the lights with the galley excepted", "turn off all the lights accept the galley",
    "turn off all the lights save for the galley", "turn off all the lights spare the galley",
    "turn off all the lights ignoring the galley", "turn off all the lights outside the galley",
    "turn off all the lights not galley",
]


@pytest.mark.parametrize("query", OTHER_WORDINGS)
def test_other_wordings_reach_the_model_not_a_fast_path(query):
    got, llm = _extract(query)
    assert llm.calls == 1, (query, got)


@pytest.mark.parametrize("query", OTHER_WORDINGS)
def test_other_wordings_never_write_the_excluded_room(monkeypatch, query):
    rig = Rig(monkeypatch, attic_state="on")
    llm = _CountingLlm()
    rig.controller.llm_router = llm
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        intent = asyncio.run(rig.controller.extract_intent(query))
    answer = asyncio.run(_execute(rig, intent, query, speaker="galley"))
    assert not set(rig.written) & GALLEY_LEAVES, (query, rig.written, answer)
    assert "light.galley_pair" not in rig.written and "light.galley_all" not in rig.written


async def _execute(rig, intent, query, *, speaker=None):
    from orchestrator import mode_permission as mp
    from orchestrator.utterance_kind import classify_utterance

    with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query), trusted_voice=True):
        return await rig.controller.execute_intent(intent, rig.raw, query, device_room=speaker)


def test_flip_the_lights_on_except_the_galley_with_the_speaker_in_the_galley(monkeypatch):
    query = "flip the lights on except the galley"
    rig = Rig(monkeypatch, attic_state="on")
    rig.controller.llm_router = _CountingLlm(json.dumps({
        "device_type": "light", "room": "whole_house", "excluded_rooms": ["galley"],
        "action": "turn_on", "target_scope": "group", "parameters": {}}))
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        intent = asyncio.run(rig.controller.extract_intent(query, device_room="galley"))
    assert rig.controller.llm_router.calls == 1, "the turn-on fast path must not answer an exclusion"
    asyncio.run(_execute(rig, intent, query, speaker="galley"))
    assert not set(rig.written) & GALLEY_LEAVES, rig.written


# ---------------------------------------------------------------------------
# A phrase used as an opener is not an exclusion
# ---------------------------------------------------------------------------

OPENERS = [
    ("aside from that, turn off all the lights in the kitchen", "kitchen"),
    ("besides the den, turn on the kitchen lights", "kitchen"),
    ("no, not the bedroom, turn on the kitchen lights", "kitchen"),
    ("apart from that, turn off the kitchen lights", "kitchen"),
]
OPENER_HOUSE = rc.RoomDirectory.build(areas=[
    Area("area_kitchen", "Kitchen", (), "kitchen"), Area("area_den", "Den", (), "den"),
    Area("area_bedroom", "Bedroom", (), "bedroom"),
])


@pytest.mark.parametrize("query,room", OPENERS)
def test_an_opener_is_not_an_exclusion(query, room):
    assert rc.has_exclusion(query, OPENER_HOUSE) is False, query
    mention = rc.find_room_mention(query, OPENER_HOUSE)
    decision = rc.decide_room(mention, query=query, speaker_room="bedroom", directory=OPENER_HOUSE)
    assert decision == rc.Room(room, room), (query, decision)


@pytest.mark.parametrize("query,room", OPENERS)
def test_an_opener_through_extract_intent_names_the_room_after_it(query, room):
    got, llm = _extract(query, directory=OPENER_HOUSE)
    assert got.get("room") == room and not got.get("excluded_rooms"), (query, got)


@pytest.mark.parametrize("query", [
    "aside from that, turn off all the lights in the galley",
    "besides the study, turn on the galley lights",
    "no, not the study, turn on the galley lights",
])
def test_an_opener_through_execute_intent_writes_the_named_room_and_not_the_rest(monkeypatch, query):
    rig = Rig(monkeypatch, attic_state="on")
    llm = _CountingLlm()
    rig.controller.llm_router = llm
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        intent = asyncio.run(rig.controller.extract_intent(query))
    assert intent.get("room") == "galley", (query, intent)
    asyncio.run(_execute(rig, intent, query, speaker="study"))
    written = set(rig.written)
    assert written and written <= GALLEY_LEAVES | {"light.galley_pair", "light.galley_all"}, (query, written)
    assert "light.study_lamp" not in written and "light.hall_lamp" not in written


def test_the_scan_still_starts_an_exclusion_after_a_scope_or_a_room():
    assert rc.has_exclusion("turn off the lights besides the galley", HOUSE)
    assert rc.has_exclusion("turn on the study lights but not the galley", HOUSE)
    assert rc.has_exclusion("everything off except the galley", HOUSE)


def test_not_the_kitchen_is_no_answer_to_a_pending_question():
    from orchestrator import room_choice

    for kind in (room_choice.KIND_ROOM, room_choice.KIND_WHICH):
        pending = {"kind": kind, "candidates": [["kitchen", "kitchen"], ["den", "den"]], "phrase": "x"}
        assert room_choice.read_reply(pending, "not the kitchen", OPENER_HOUSE).kind == "none", kind
        assert room_choice.read_reply(pending, "the kitchen", OPENER_HOUSE).kind == "one", kind


# ---------------------------------------------------------------------------
# One guard, asked before any fast path
# ---------------------------------------------------------------------------

def test_the_fast_paths_sit_behind_the_one_guard():
    source = (SRC / "orchestrator" / "smart_home_controller.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SmartHomeController")
    methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    refs = lambda node, attr: [c for c in ast.walk(node) if isinstance(c, ast.Attribute) and c.attr == attr]  # noqa: E731
    callers = [m for m, node in methods.items() if refs(node, "_fast_path_intent")]
    assert callers == ["extract_intent"], callers
    assert len(refs(methods["extract_intent"], "_fast_path_intent")) == 1
    extract = methods["extract_intent"]
    guard = next(n for n in ast.walk(extract) if isinstance(n, ast.If) and refs(n.test, "_defers_to_model"))
    assert refs(guard, "_fast_path_intent") and guard.test is not None
    returns = [n for n in ast.walk(extract) if isinstance(n, ast.Return)]
    assert returns, "extract_intent returns something"
    early = [n.lineno for n in returns if n.lineno < guard.lineno]
    assert early == [], f"extract_intent answers before asking _defers_to_model: lines {early}"
    assert sum(1 for n in ast.walk(methods["_fast_path_intent"]) if isinstance(n, ast.Return)) >= 20


def test_the_pin_would_catch_an_early_return():
    planted = ast.parse("async def extract_intent(self, q):\n    if q:\n        return {}\n"
                        "    if not self._defers_to_model(q):\n        return await self._fast_path_intent(q)\n").body[0]
    guard = next(n for n in ast.walk(planted) if isinstance(n, ast.If) and any(
        isinstance(c, ast.Attribute) and c.attr == "_defers_to_model" for c in ast.walk(n.test)))
    assert [n.lineno for n in ast.walk(planted) if isinstance(n, ast.Return) and n.lineno < guard.lineno] == [3]


def test_a_porch_exclusion_in_either_word_order_never_turns_the_porch_off(monkeypatch):
    house = rc.RoomDirectory.build(areas=[Area("area_porch", "Porch", (), "porch"), Area("area_den", "Den", (), "den")])
    for query in ("turn off all the lights, leave the porch on", "turn off all the lights and keep the porch on"):
        got, llm = _extract(query, llm=_CountingLlm(json.dumps({
            "device_type": "light", "room": "whole_house", "excluded_rooms": ["porch"],
            "action": "turn_off", "target_scope": "group", "parameters": {}})), directory=house)
        assert llm.calls == 1, query
        assert got["room"] == "whole_house" and got["excluded_rooms"] == ["porch"], (query, got)


# ---------------------------------------------------------------------------
# R1: a leading phrase with a whole-house cue is an exclusion
# ---------------------------------------------------------------------------

FRONT = [p for p in PHRASINGS]


@pytest.mark.parametrize("phrasing", FRONT)
@pytest.mark.parametrize("tail", ["turn off all the lights", "turn off every light", "turn everything off"])
def test_a_leading_phrase_with_a_whole_house_cue_excludes_the_room(phrasing, tail):
    query = f"{phrasing} the galley, {tail}"
    assert rc.has_exclusion(query, HOUSE), query
    mention = rc.find_room_mention(query, HOUSE)
    decision = rc.decide_room(mention, query=query, speaker_room="galley", directory=HOUSE)
    assert decision == rc.WholeHouse((GALLEY,)), (query, decision)
    assert _controller()._defers_to_model(query) is False, "the room parser reads it, so no model is needed"


@pytest.mark.parametrize("phrasing", FRONT)
def test_a_leading_phrase_never_writes_the_galley(monkeypatch, phrasing):
    query = f"{phrasing} the galley, turn off all the lights"
    rig = Rig(monkeypatch, attic_state="on")
    intent = {"device_type": "light", "room": "whole_house", "action": "turn_off", "target_scope": "group",
              "parameters": {}}
    asyncio.run(_execute(rig, intent, query, speaker="galley"))
    assert not set(rig.written) & GALLEY_LEAVES, (query, rig.written)
    assert "light.galley_pair" not in rig.written and "light.galley_all" not in rig.written
    assert "light.study_lamp" in rig.written, "the rest of the house is turned off"


def test_without_a_cue_a_leading_phrase_is_still_an_opener():
    assert rc.has_exclusion("besides the den, turn on the kitchen lights", OPENER_HOUSE) is False
    assert rc.has_exclusion("aside from that, turn off all the lights in the kitchen", OPENER_HOUSE) is False


# ---------------------------------------------------------------------------
# R2: deferral words need a room, or what reads as one, after them
# ---------------------------------------------------------------------------

CASUAL = [
    "turn off all the lights before we leave", "keep all the lights off",
    "turn everything off, save power", "turn off everything outside",
    "turn everything off and keep them off", "turn off all the lights, leave it off",
]
SPARING = [
    "turn off all the lights, leave the porch on", "turn off all the lights and keep the hall lit",
    "turn off all the lights, save the study", "turn off all the lights keep the porch on",
]


@pytest.mark.parametrize("query", CASUAL)
def test_casual_use_of_a_deferral_word_stays_on_its_fast_path(query):
    got, llm = _extract(query)
    assert llm.calls == 0, (query, got)
    assert got.get("room") == "whole_house" or got.get("action") in ("turn_off", "turn_on"), (query, got)
    assert rc.decide_room(rc.find_room_mention(query, HOUSE), query=query, speaker_room="study",
                          directory=HOUSE) != rc.AskRoom(), query


@pytest.mark.parametrize("query", SPARING)
def test_a_deferral_word_before_a_room_or_what_reads_as_one_reaches_the_model(query):
    got, llm = _extract(query)
    assert llm.calls == 1, (query, got)


def test_a_deferral_word_that_is_a_rooms_own_name_is_a_room():
    outside = rc.RoomDirectory.build(areas=[Area("area_outside", "Outside", (), "outside"), Area("area_den", "Den", (), "den")])
    got, llm = _extract("turn off everything outside", directory=outside)
    assert llm.calls == 0 and got.get("room") != "whole_house" or llm.calls == 0, got
    assert rc.has_exclusion_deferral_word("turn off all the lights outside the den", outside) is False
    assert rc.has_exclusion_deferral_word("turn off all the lights outside the galley", HOUSE) is True
    assert rc.has_exclusion_deferral_word("turn off the outside lights", outside) is False


# ---------------------------------------------------------------------------
# R3: the model's exclusions are used only when they cover the utterance
# ---------------------------------------------------------------------------

def _model_whole_house(rig, excluded):
    rig.controller.llm_router = _CountingLlm(json.dumps({
        "device_type": "light", "room": "whole_house", "excluded_rooms": excluded,
        "action": "turn_off", "target_scope": "group", "parameters": {}}))


def _run_model_case(monkeypatch, query, excluded):
    rig = Rig(monkeypatch, attic_state="on")
    _model_whole_house(rig, excluded)
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        intent = asyncio.run(rig.controller.extract_intent(query))
    answer = asyncio.run(_execute(rig, intent, query, speaker="hall"))
    return rig, answer


def test_a_model_that_names_an_unknown_room_gets_an_ask_and_no_writes(monkeypatch):
    rig, answer = _run_model_case(monkeypatch, "turn off all the lights, leave the galley on", ["sunroom"])
    assert rig.written == [], rig.written


def test_a_model_list_that_misses_a_named_room_gets_an_ask_and_no_writes(monkeypatch):
    rig, answer = _run_model_case(monkeypatch, "turn off all the lights, leave the galley and the study on", ["galley"])
    assert rig.written == [], rig.written


def test_a_model_list_that_covers_every_named_room_turns_off_the_rest(monkeypatch):
    rig, answer = _run_model_case(monkeypatch, "turn off all the lights, leave the galley and the study on",
                                  ["galley", "study"])
    assert "light.hall_lamp" in rig.written and "light.north_lamp" in rig.written, rig.written
    assert not set(rig.written) & GALLEY_LEAVES and "light.study_lamp" not in rig.written


def test_grounded_exclusion_rules_in_isolation():
    q = "turn off all the lights, leave the galley and the study on"
    assert rc.grounded_exclusion(q, ["galley", "study"], HOUSE) == (GALLEY, rc.RoomRef("study", "study"))
    assert rc.grounded_exclusion(q, ["galley"], HOUSE) is None
    assert rc.grounded_exclusion(q, ["galley", "sunroom"], HOUSE) is None
    assert rc.grounded_exclusion("leave the galley on", ["galley", "sunroom"], HOUSE) is None
    assert rc.grounded_exclusion("leave the galley on", ["galley"], HOUSE) == (GALLEY,)
    assert rc.grounded_exclusion(q, "galley", HOUSE) is None
    assert rc.grounded_exclusion(q, [], HOUSE) is None
    assert rc.grounded_exclusion(q, [7], HOUSE) is None


# ---------------------------------------------------------------------------
# R4: "leave X on but turn off the rest"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("joiner", ["but", "and", "then"])
@pytest.mark.parametrize("what", ["the galley", "the galley lights"])
def test_leave_on_form_writes_every_room_but_the_one_kept(monkeypatch, joiner, what):
    query = f"leave {what} on {joiner} turn off the rest"
    rig = Rig(monkeypatch, attic_state="on")
    rig.controller.llm_router = None
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        intent = asyncio.run(rig.controller.extract_intent(query))
    assert intent["room"] == "whole_house" and intent["excluded_rooms"] == ["galley"], (query, intent)
    asyncio.run(_execute(rig, intent, query, speaker="galley"))
    assert not set(rig.written) & GALLEY_LEAVES and "light.galley_all" not in rig.written, rig.written
    assert "light.study_lamp" in rig.written and "light.hall_lamp" in rig.written


def test_leave_on_form_is_read_from_the_words_alone(monkeypatch):
    query = "leave the galley on but turn off the rest"
    assert rc.decide_room(rc.find_room_mention(query, HOUSE), query=query, speaker_room="study",
                          directory=HOUSE) == rc.WholeHouse((GALLEY,))


def test_leave_on_form_with_other_exclusion_wording_is_not_trusted():
    query = "leave the galley on but turn off the rest except the study"
    assert rc.leave_on_exclusion(query, HOUSE) == ()
    assert _controller()._defers_to_model(query) is True
    assert rc.decide_room(rc.find_room_mention(query, HOUSE), query=query, speaker_room="study",
                          directory=HOUSE) == rc.AskRoom()


def test_leave_on_form_with_other_exclusion_wording_writes_nothing(monkeypatch):
    query = "leave the galley on but turn off the rest except the study"
    rig = Rig(monkeypatch, attic_state="on")
    intent = {"device_type": "light", "room": None, "action": "turn_off", "target_scope": "group", "parameters": {}}
    asyncio.run(_execute(rig, intent, query, speaker="hall"))
    assert rig.written == [], rig.written


def test_an_exclusion_with_no_whole_house_wording_never_defaults_to_the_speakers_room(monkeypatch):
    query = "flip the lights on except the galley"
    rig = Rig(monkeypatch, attic_state="on")
    intent = {"device_type": "light", "room": None, "action": "turn_on", "target_scope": "group", "parameters": {}}
    answer = asyncio.run(_execute(rig, intent, query, speaker="galley"))
    assert rig.written == [], (rig.written, answer)


def test_a_grounded_model_exclusion_turns_off_the_rest_of_the_house(monkeypatch):
    query = "turn off all the lights, leave the galley on"
    rig = Rig(monkeypatch, attic_state="on")
    intent = {"device_type": "light", "room": "whole_house", "excluded_rooms": ["galley"],
              "action": "turn_off", "target_scope": "group", "parameters": {}}
    asyncio.run(_execute(rig, intent, query, speaker="study"))
    assert "light.study_lamp" in rig.written and "light.hall_lamp" in rig.written, rig.written
    assert not set(rig.written) & GALLEY_LEAVES and "light.galley_all" not in rig.written


def test_an_exclusion_naming_no_known_room_writes_nothing_through_execute_intent(monkeypatch):
    query = "turn off all the lights except the sunroom"
    rig = Rig(monkeypatch, attic_state="on")
    intent = {"device_type": "light", "room": "whole_house", "action": "turn_off", "target_scope": "group",
              "parameters": {}}
    answer = asyncio.run(_execute(rig, intent, query, speaker="galley"))
    assert rig.written == [], rig.written
    assert answer, "the speaker is told something"


def test_a_wording_the_parser_cannot_read_and_a_model_that_names_no_room_writes_nothing(monkeypatch):
    query = "turn off all the lights spare the galley"
    rig = Rig(monkeypatch, attic_state="on")
    intent = {"device_type": "light", "room": "whole_house", "action": "turn_off", "target_scope": "group",
              "parameters": {}}
    asyncio.run(_execute(rig, intent, query, speaker="galley"))
    assert rig.written == [], rig.written


def test_an_unknown_excluded_room_asks_instead_of_writing_the_whole_house():
    got = rc.decide_room(rc.NO_MENTION, query="turn off all the lights except the sunroom",
                         speaker_room="study", directory=HOUSE)
    assert got == rc.AskRoom()


def test_apart_from_is_read_as_an_exclusion_not_as_the_room_it_precedes():
    _runtime.set_room_directory(type("P", (), {"current": lambda self: HOUSE})())

    class Admin:
        async def get_component_model(self, name):
            return None

    ctl = shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=None)
    with mock.patch.object(shc, "get_admin_client", lambda: Admin()):
        got = asyncio.run(ctl.extract_intent("turn off every light apart from the galley"))
    assert got["room"] == "whole_house" and got["excluded_rooms"] == ["galley"], got


# ---------------------------------------------------------------------------
# End to end: the excluded room's lights are never written
# ---------------------------------------------------------------------------

QUERY = "get everything dark apart from the galley"


class _Admin:
    async def get_component_model(self, name):
        return None


def test_x03_perfect_json_writes_nothing_on_the_excluded_room(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on")
    perfect = json.dumps({"device_type": "light", "room": "whole_house", "excluded_rooms": ["galley"],
                          "action": "turn_off", "target_scope": "group", "parameters": {},
                          "color_description": None})

    class Llm:
        async def generate(self, **kwargs):
            return {"response": perfect}

    rig.controller.llm_router = Llm()
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        intent = asyncio.run(rig.controller.extract_intent(QUERY))
    assert intent["excluded_rooms"] == ["galley"], intent
    rig.run(intent, QUERY)
    assert rig.written, "the rest of the house is turned off"
    assert not set(rig.written) & GALLEY_LEAVES, rig.written
    assert "light.galley_pair" not in rig.written and "light.galley_all" not in rig.written


def test_x10_the_json_fallback_intent_writes_nothing_on_the_excluded_room(monkeypatch):
    """What extract_intent returns when the model's reply is not JSON: no room,
    no exclusions. The exclusion in the words themselves still holds."""
    rig = Rig(monkeypatch, attic_state="on")
    fallback = {"device_type": "light", "room": None, "action": "turn_off", "target_scope": "group", "parameters": {}}
    rig.run(fallback, "turn off everything apart from the galley")
    assert not set(rig.written) & GALLEY_LEAVES, rig.written
    assert "light.galley_pair" not in rig.written and "light.galley_all" not in rig.written


# ---------------------------------------------------------------------------
# The prompt and the music handler name the same vocabulary
# ---------------------------------------------------------------------------

def test_the_intent_prompt_names_every_phrase():
    golden = (Path(__file__).parent / "fixtures" / "smart_home_intent_prompt.golden.txt").read_text()
    line = next(l for l in golden.splitlines() if l.startswith("EXCLUSIONS:"))
    for phrase in rc.EXCLUSION_PHRASES:
        assert f'"{" ".join(phrase)} [room]"' in line, phrase


def test_the_music_exclusion_regex_is_built_from_the_vocabulary():
    import orchestrator.music_handler as mh

    alternatives = mh._EXCLUSION_ALTERNATION.split("|")
    assert len(alternatives) == len(rc.EXCLUSION_PHRASES)
    assert alternatives[0] == max(alternatives, key=len), "longest phrase first"
    assert alternatives.index(r"but\s+not") < alternatives.index("but")
    assert alternatives.index("excepting") < alternatives.index("except")


@pytest.mark.parametrize("phrasing", ["except", "excepting", "but not", "but", "other than", "excluding",
                                      "apart from", "aside from", "besides", "not including", "not the"])
def test_music_excludes_the_room_after_every_phrase(phrasing):
    import orchestrator.music_handler as mh

    handler = mh.MusicHandler.__new__(mh.MusicHandler)
    configs = {"office": {}, "kitchen": {}}
    got = asyncio.run(handler._parse_exclusion_pattern(
        f"play jazz everywhere {phrasing} the office", configs))
    assert got == ["office"], (phrasing, got)


def _music_parse(query, configs):
    import orchestrator.music_handler as mh

    handler = mh.MusicHandler.__new__(mh.MusicHandler)
    return asyncio.run(handler._parse_exclusion_pattern(query, configs))


MUSIC_ROOMS = {"office": {}, "kitchen": {}, "living_room": {}, "master_bedroom": {"display_name": "Primary Suite"}}


def test_music_reads_except_for_and_the_phrases_with_for():
    assert _music_parse("play jazz everywhere except for the office", MUSIC_ROOMS) == ["office"]
    assert _music_parse("play jazz in all rooms apart from the kitchen", MUSIC_ROOMS) == ["kitchen"]
    assert _music_parse("play jazz besides for the office", MUSIC_ROOMS) == ["office"]


def test_music_captures_multi_word_rooms():
    assert _music_parse("play jazz everywhere except the living room", MUSIC_ROOMS) == ["living_room"]
    assert _music_parse("play jazz except the living room and the office", MUSIC_ROOMS) == ["living_room", "office"]
    assert _music_parse("play jazz except the primary suite", MUSIC_ROOMS) == ["master_bedroom"]
    assert _music_parse("play jazz except the office and play it loud", MUSIC_ROOMS) == ["office"]


@pytest.mark.parametrize("query", [
    "play the debut album everywhere", "play a rebut podcast in the office", "play abut the office",
    "play jazz in the office",
])
def test_music_phrases_need_a_left_word_boundary(query):
    assert _music_parse(query, MUSIC_ROOMS) == [], query


def test_music_falls_through_to_the_second_pattern_when_the_first_parses_no_rooms():
    query = "play rock except the office, everywhere except the sunroom"
    assert _music_parse(query, MUSIC_ROOMS) == ["office"]


def test_music_logs_a_count_not_room_keys():
    import orchestrator.music_handler as mh

    with mock.patch.object(mh.logger, "info") as info:
        _music_parse("play jazz everywhere except the office", MUSIC_ROOMS)
    kwargs = info.call_args.kwargs
    assert kwargs.get("excluded_count") == 1 and "excluded" not in kwargs


# ---------------------------------------------------------------------------
# Drift: no second list of the exclusion words in the orchestrator
# ---------------------------------------------------------------------------

PHRASE_STRINGS = frozenset(" ".join(p) for p in rc.EXCLUSION_PHRASES)

# The vocabulary's own home is not a second copy of itself.
DEFINITION_FILE = "src/orchestrator/room_context.py"

SCAN_ROOT = "src"

# (path, enclosing name) -> why it is not an exclusion parser.
ALLOWLIST = {
    ("src/orchestrator/validator.py", "_extract_key_entities"):
        "stopword list of a validator; 'but' there is a function word",
    ("src/orchestrator/context/detector.py", "CONTEXT_REF_PATTERNS"):
        "correction cue: 'but' marks a follow-up, not a room exclusion",
    ("src/orchestrator/context/detector.py", "_FOLLOW_UP_LEADING_RE"):
        "a follow-up opener ('but ...'), not a room exclusion",
    ("src/orchestrator/complexity_detector.py", "extract_complexity_features"):
        "counts conjunctions to estimate how complex a request is",
    ("src/orchestrator/semantic_cache.py", "UNCACHEABLE_PATTERNS"):
        "a query with 'except' is never answered from the semantic cache; control answers are not cached at all",
    ("src/orchestrator/smart_home_controller.py", "_EVERYTHING_BUT_RE"):
        "'everything but X' regex kept beside the vocabulary guard; the room parser reads the exclusion itself",
}

_RE_CALLS = frozenset({"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "split"})


def _norm(text: str) -> str:
    text = re.sub(r"\\b", " ", text)
    return " ".join(re.sub(r"\\s[+*]", " ", text).lower().split())


def _is_phrase(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and _norm(node.value) in PHRASE_STRINGS


def _enclosing(stack):
    for node in reversed(stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    for node in reversed(stack):
        if isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return node.target.id
    return "<module>"


def _regex_has_phrase(pattern: str) -> bool:
    text = _norm(re.sub(r"[()?:^$|\[\]]", " ", pattern))
    padded = f" {text} "
    return any(f" {phrase} " in padded for phrase in PHRASE_STRINGS)


def _is_re_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Attribute) and node.func.attr in _RE_CALLS)
        or (isinstance(node.func, ast.Name) and node.func.id in _RE_CALLS)
    )


def scan_source(source: str):
    """(line, enclosing name, kind): a phrase literal that is a member of a
    list/tuple/set, a direct operand of ``in``, part of a regex alternation of
    two or more phrases, or a whole word of a pattern passed to ``re``."""
    findings = []

    def visit(node, stack):
        stack = stack + [node]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and any(_is_phrase(e) for e in node.elts):
            findings.append((node.lineno, _enclosing(stack), "member"))
        elif isinstance(node, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in node.ops):
            if any(_is_phrase(x) for x in [node.left, *node.comparators]):
                findings.append((node.lineno, _enclosing(stack), "operand"))
        elif _is_re_call(node):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _regex_has_phrase(arg.value):
                    findings.append((node.lineno, _enclosing(stack), "regex-arg"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "|" in node.value:
            parts = [_norm(re.sub(r"[()?:^$]", "", part)) for part in node.value.split("|")]
            if sum(1 for part in parts if part in PHRASE_STRINGS) >= 2:
                findings.append((node.lineno, _enclosing(stack), "alternation"))
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(ast.parse(source), [])
    return findings


# Files under src/ that do not parse at the base commit; the scan cannot read
# them. Compared exactly, so a fixed (or newly broken) file is noticed.
UNPARSEABLE = {"src/jetson/athena_lite_llm.py"}


def _tree_findings(unparseable=None):
    found = {}
    for path in sorted((ROOT / SCAN_ROOT).rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == DEFINITION_FILE:
            continue
        try:
            findings = scan_source(path.read_text(encoding="utf-8"))
        except SyntaxError:
            if unparseable is not None:
                unparseable.add(rel)
            continue
        for line, name, kind in findings:
            found.setdefault((rel, name), []).append((line, kind))
    return found


def test_the_files_the_scan_cannot_parse_are_the_known_ones():
    seen = set()
    _tree_findings(seen)
    assert seen == UNPARSEABLE


def test_no_second_exclusion_list_outside_the_allowlist():
    unexpected = {k: v for k, v in _tree_findings().items() if k not in ALLOWLIST}
    assert not unexpected, f"exclusion words listed outside room_context.EXCLUSION_PHRASES: {unexpected}"


def test_every_allowlist_entry_still_matches_a_finding():
    stale = [key for key in ALLOWLIST if key not in _tree_findings()]
    assert not stale, f"allowlist entries that match nothing (delete them): {stale}"


def test_the_scan_floor_sees_real_code_in_at_least_two_files():
    assert len({path for path, _ in _tree_findings()}) >= 2
    assert len(list((ROOT / SCAN_ROOT / "orchestrator").rglob("*.py"))) > 20
    scanned = {p.relative_to(ROOT).parts[1] for p in (ROOT / SCAN_ROOT).rglob("*.py")}
    assert {"orchestrator", "gateway", "shared"} <= scanned, "the scan reaches beyond the orchestrator"


def test_the_scanner_reports_a_planted_list():
    assert scan_source("def f(q):\n    return any(p in q for p in ['except', 'but not'])\n") == [(2, "f", "member")]


def test_the_scanner_reports_a_planted_in_operand():
    assert scan_source("def f(q):\n    return 'apart from' in q\n") == [(2, "f", "operand")]


def test_the_scanner_reports_a_planted_regex_alternation():
    assert scan_source('PATTERN = "(?:except|besides|other\\\\s+than)\\\\s+x"\n') == [(1, "PATTERN", "alternation")]


def test_the_scanner_reports_a_word_boundary_literal():
    assert scan_source("def f(q):\n    return r'\\bapart from\\b' in q\n") == [(2, "f", "operand")]


def test_the_scanner_reports_a_phrase_inside_a_re_call():
    source = "import re\nPAT = re.compile(r'turn off (?:all|every)\\s+but\\s+x')\ndef f(q):\n    return re.search(r'besides\\s+the', q)\n"
    assert scan_source(source) == [(2, "PAT", "regex-arg"), (4, "f", "regex-arg")]


def test_the_scanner_ignores_a_phrase_inside_a_longer_word_in_a_re_call():
    assert scan_source("import re\nPAT = re.compile(r'rebut|debut|abutment')\n") == []


def test_an_allowlist_entry_names_a_function_or_assignment_not_the_whole_module():
    assert all(name != "<module>" for _, name in ALLOWLIST)


def test_the_scanner_ignores_unrelated_literals():
    assert scan_source("a = ['red', 'green']\nb = 'press' in 'the button'\nc = 'a|b'\n") == []

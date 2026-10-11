"""Where a command acts is decided once and recorded on the intent; replays,
follow-ups and sequences carry it and never decide again."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from . import _room_chat_harness as rch
from ._room_chat_harness import Chat
from .test_room_light_area_tier import _isolated  # noqa: F401

from orchestrator import room_choice  # noqa: E402
from orchestrator.state import ConversationContext  # noqa: E402

STUDY = {f"light.study_lamp_{i}" for i in range(3)}
GALLEY = {"light.galley_lamp_0", "light.galley_lamp_1"}
SOURCE = Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "smart_home_controller.py"


def test_a_command_records_where_it_acted_as_plain_strings(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("the lights please")
    stored = chat.store["s1"]["parameters"]
    assert stored["decided_room"] == "study" and stored["room_label"] == "study"
    assert stored["decided_rooms"] == [] and stored["decided_excluded"] == []
    context = ConversationContext(timestamp=1.0, **chat.store["s1"])
    again = json.loads(context.model_dump_json())["parameters"]
    assert again["decided_room"] == "study"
    assert all(isinstance(v, (str, list, int, float, bool, type(None), dict)) for v in again.values())


def test_a_pending_write_replayed_from_another_room_writes_the_stored_room(monkeypatch):
    chat = Chat(monkeypatch, threshold=1)
    first = chat.turn("the lights please")
    assert chat.written == [] and first.answer.endswith("?")
    chat.turn("yes", room="galley")
    assert set(chat.written) == STUDY, "the replay carried study; it did not decide galley"


def test_a_pending_stored_before_this_change_decides_once_and_a_mismatch_asks_again(monkeypatch):
    chat = Chat(monkeypatch, threshold=1)
    first = chat.turn("the lights please")
    stored = chat.store["s1"]
    pending = stored["parameters"]["pending_write_confirmation"]
    for key in ("decided_room", "decided_rooms", "decided_excluded", "room_label"):
        pending["intent"].pop(key, None)
    pending["intent"]["room"] = None  # what the image before this change stored
    out = chat.turn("yes", room="galley")
    assert chat.written == [] and out.answer.endswith("?"), (
        "decided in the galley, which the confirmed set (the study) doesn't cover: asked again, nothing written"
    )


def test_executing_without_the_users_words_never_records_a_room_question(monkeypatch):
    chat = Chat(monkeypatch)
    intent = {"device_type": "light", "room": "bathroom", "action": "turn_on",
              "target_scope": "group", "parameters": {}}

    async def go():
        from orchestrator import mode_permission as mp
        from orchestrator.nodes import route_control

        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", trusted_voice=True):
            text = await chat.rig.controller.execute_intent(intent, chat.rig.raw, original_query=None, device_room="study")
            return text, room_choice.take()

    text, recorded = asyncio.run(go())
    assert text == "Which bathroom: lower, north or south?" and recorded is None
    assert chat.written == []


def test_the_sequence_prompt_does_not_tell_the_model_where_the_speaker_is(monkeypatch):
    chat = Chat(monkeypatch)
    chat.llm.reply = {"type": "sequence", "steps": []}
    prompts = []
    real = chat.llm.generate

    async def spy(**kwargs):
        prompts.append(kwargs["prompt"])
        return await real(**kwargs)

    chat.llm.generate = spy
    asyncio.run(chat.rig.controller.extract_sequence_intent("blink the lights", device_room="study"))
    assert "Current room" not in prompts[0] and "study" not in prompts[0].lower()


def sequence(steps, acknowledge="Okay."):
    return {"type": "sequence", "acknowledge": acknowledge, "steps": steps}


def step(room, action="turn_on", device_type="light"):
    return {"action": action, "target": {"device_type": device_type, "room": room}, "parameters": {},
            "delay_after": 0, "at_time": None}


def run_sequence(chat, query, steps):
    from orchestrator.nodes import _runtime

    scheduled = []

    class Executor:
        async def execute_sequence(self, steps, session_id=None, background=False):
            scheduled.append(steps)
            return []

        def __bool__(self):
            return True

    _runtime.set_sequence_executor(Executor())
    chat.rig.controller.detect_sequence_intent = lambda q: True

    async def extract(q, device_room=None):
        return sequence(steps)

    chat.rig.controller.extract_sequence_intent = extract
    out = chat.turn(query)
    return out, scheduled


def test_a_sequence_step_for_the_whole_house_from_words_that_do_not_say_so_is_refused_with_the_question(monkeypatch):
    chat = Chat(monkeypatch)
    out, scheduled = run_sequence(chat, "blink the lights", [step("whole_house")])
    assert out.answer == "Which room?" and scheduled == []
    assert "awaiting_room_choice" not in chat.stored_parameters()


def test_a_sequence_step_for_the_whole_house_is_allowed_when_the_words_say_so(monkeypatch):
    chat = Chat(monkeypatch)
    out, scheduled = run_sequence(chat, "blink all the lights", [step("whole_house")])
    assert scheduled and scheduled[0][0]["target"]["decided_room"] == "whole_house"


def test_a_sequence_with_an_ambiguous_step_room_asks_and_schedules_nothing_and_stores_nothing(monkeypatch):
    chat = Chat(monkeypatch)
    out, scheduled = run_sequence(chat, "blink the bathroom lights", [step("study"), step("bathroom")])
    assert out.answer == "Which bathroom: lower, north or south?" and scheduled == []
    assert chat.stores == [], "a sequence never carries a pending"


def test_every_scheduled_step_carries_its_decision(monkeypatch):
    chat = Chat(monkeypatch)
    out, scheduled = run_sequence(chat, "blink the lights then the galley lights", [step(None), step("galley")])
    decided = [s["target"]["decided_room"] for s in scheduled[0]]
    assert decided == ["study", "galley"], "a step with no room gets the speaker's, decided here"


def test_the_executor_hands_the_decision_to_execute_intent():
    source = (Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "sequence_executor.py").read_text()
    assert "decided_room" in source


def test_the_speaker_room_default_is_applied_in_one_place():
    text = SOURCE.read_text()
    assert len(re.findall(r"device_room not in", text)) == 1, "only the out-of-scope motion prompt remains"


def test_the_scope_cue_compares_whole_words_not_substrings():
    from orchestrator import write_fanout

    cue = write_fanout._has_explicit_scope_cue
    assert cue("turn on the garden lights", ("room_group", "den")) is False
    assert cue("turn on the den lights", ("room_group", "den")) is True
    assert cue("turn on the living room lights", ("room_group", "living_room")) is True
    assert cue("lights in the garden and the study", ("multi_room", ["den", "study"])) is False
    assert cue("lights in the den and the study", ("multi_room", ["den", "study"])) is True


def test_the_replay_reads_the_first_turn_words_not_the_replys(monkeypatch):
    chat = Chat(monkeypatch)
    seen = []
    real = chat.rig.controller.execute_intent

    async def spy(intent, ha_client, original_query=None, **kw):
        seen.append(original_query)
        return await real(intent, ha_client, original_query=original_query, **kw)

    monkeypatch.setattr(chat.rig.controller, "execute_intent", spy)
    chat.turn("turn on the bathroom lights")
    chat.turn("north")
    assert seen == ["turn on the bathroom lights", "turn on the bathroom lights"], seen


# ---------------------------------------------------------------------------
# "All the lights" for a proven owner
# ---------------------------------------------------------------------------

def _audience(**kw):
    from shared.knowledge_tiers import KnowledgeAudience

    fields = dict(mode="owner", degraded=False, public=False, owner_caller=True, owner_proven=True)
    fields.update(kw)
    return KnowledgeAudience(**fields)


def _all_lights(chat, **state_kw):
    audience = state_kw.pop("audience", None)
    state = chat.state("turn off all the lights", **state_kw)
    if audience is not None:
        state.knowledge_audience = audience
    with chat.patched():
        return asyncio.run(rch.route_control_node(state))


ALL_LAMPS = {f"light.{k}_lamp_{i}" for k, n in rch.LAMPS.items() for i in range(n)}


def test_a_proven_owner_without_a_trusted_device_reaches_every_area(monkeypatch):
    chat = Chat(monkeypatch)
    _all_lights(chat, trusted_voice=False, audience=_audience())
    assert set(chat.written) == ALL_LAMPS


def test_a_trusted_device_alone_still_reaches_every_area(monkeypatch):
    chat = Chat(monkeypatch)
    _all_lights(chat, trusted_voice=True)
    assert set(chat.written) == ALL_LAMPS


@pytest.mark.parametrize("audience", [
    None,
    _audience(owner_proven=False),                       # a signed-in owner whose mode isn't proven
    _audience(owner_caller=False, owner_proven=False),   # household, PIN override, web_local ...
    _audience(mode="guest", owner_caller=False, owner_proven=False),
])
def test_an_untrusted_caller_who_is_not_a_proven_owner_gets_the_groups_only(monkeypatch, audience):
    chat = Chat(monkeypatch)
    _all_lights(chat, trusted_voice=False, audience=audience)
    assert not (set(chat.written) & ALL_LAMPS), "no group is configured, so nothing area-wide goes"


# ---------------------------------------------------------------------------
# A room that is named but unknown is not the speaker's room
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,phrase", [
    ("turn on the sunroom lights", "sunroom"),
    ("turn on sunroom lights", "sunroom"),
    ("turn on the lights in the sunroom", "sunroom"),
    ("turn off all the lights in the sunroom", "sunroom"),
    ("turn on the lights in the sunroom please", "sunroom"),
])
def test_a_named_room_the_house_does_not_have_writes_nothing_and_says_so(monkeypatch, query, phrase):
    chat = Chat(monkeypatch)
    out = chat.turn(query)
    assert chat.written == [], (query, chat.written)
    assert out.answer == f"I couldn't find a room called {phrase}.", out.answer
    assert "study" not in out.answer and "galley" not in out.answer, "no room list leaks"


@pytest.mark.parametrize("query", [
    "turn on the lights",
    "turn on the red lights",
    "turn on dim lights",
    "dim the lights",
    "turn on the ceiling lights",
    "turn the lights to 50 percent",
    "turn on the lights in here",
    "turn on the lights at sunset",
    "turn on the bedside lamps",
])
def test_a_command_with_no_room_still_uses_the_speakers_room(monkeypatch, query):
    chat = Chat(monkeypatch)
    out = chat.turn(query)
    assert set(chat.written) == STUDY, (query, chat.written, out.answer)


def test_an_unknown_room_from_an_unknown_speaker_room_is_still_not_a_question_or_a_write(monkeypatch):
    chat = Chat(monkeypatch)
    out = chat.turn("turn on the sunroom lights", room="unknown")
    assert chat.written == [] and out.answer == "I couldn't find a room called sunroom."


def test_a_known_room_beats_the_unknown_check(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("turn on the galley lights")
    assert set(chat.written) == GALLEY


def test_a_question_about_an_unknown_room_is_not_answered_for_the_speakers_room(monkeypatch):
    chat = Chat(monkeypatch)
    chat.llm.reply = {"device_type": "light", "room": None, "action": "get_status",
                      "target_scope": "group", "parameters": {}}
    out = chat.turn("are the lights on in the sunroom")
    assert out.answer == "I couldn't find a room called sunroom."


def test_a_room_the_directory_lacks_is_still_found_by_light_names_when_lights_answer_to_it(monkeypatch):
    chat = Chat(monkeypatch, registry=False)
    chat.rig.manager._entities_cache["light.gazebo_lamp"] = {
        "entity_id": "light.gazebo_lamp", "state": "off", "attributes": {"friendly_name": "Gazebo Lamp"}}
    chat.rig.manager._build_indexes()
    chat.turn("turn on the gazebo lights")
    assert chat.written == ["light.gazebo_lamp"]

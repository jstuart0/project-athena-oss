"""'Which one?': the question, the pending that carries it, and how the
answer completes (or doesn't complete) the command.

A multi-turn rig over a fictional house (North/South/Lower Bathroom, Study,
Galley); see _room_chat_harness.py. Names read as specs.
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest

from . import _room_chat_harness as rch
from ._room_chat_harness import Chat, guest
from .test_room_light_area_tier import _isolated  # noqa: F401  (autouse fixture)

from orchestrator import fast_path, helpers, room_choice, write_fanout  # noqa: E402
from orchestrator.nodes import _runtime, route_control  # noqa: E402
from orchestrator.nodes.route_control import DECLINED_TEXT, REPLY_AGAIN_TEXT  # noqa: E402

QUESTION = "Which bathroom: lower, north or south?"
NORTH = {"light.north_lamp_0", "light.north_lamp_1"}
SOUTH = {"light.south_lamp_0", "light.south_lamp_1"}
STUDY = {f"light.study_lamp_{i}" for i in range(3)}


def ask_bathroom(chat, **kw):
    out = chat.turn("turn on the bathroom lights", **kw)
    return out


# ---------------------------------------------------------------------------
# The question and the pending
# ---------------------------------------------------------------------------

def test_bathroom_from_the_study_asks_which_writes_nothing_and_stores_the_pending(monkeypatch):
    chat = Chat(monkeypatch)
    out = ask_bathroom(chat)
    assert out.answer == QUESTION and chat.written == []
    params = chat.stored_parameters()
    assert set(params) >= {"awaiting_room_choice"}
    assert "pending_write_confirmation" not in params and "awaiting_state_question" not in params
    pending = params["awaiting_room_choice"]
    assert pending["fingerprint"] == "fp-a" and pending["query"] == "turn on the bathroom lights"
    assert sorted(c[0] for c in pending["candidates"]) == ["lower_bathroom", "north_bathroom", "south_bathroom"]
    assert pending["intent"].get("decided_room") is None, "nothing is decided yet"


def test_the_pending_is_json_with_plain_strings_only(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    from orchestrator.state import ConversationContext

    stored = chat.store["s1"]
    context = ConversationContext(timestamp=1.0, **stored)
    again = json.loads(context.model_dump_json())
    assert again["parameters"]["awaiting_room_choice"]["nonce"]


def test_north_from_the_same_caller_completes_the_command_for_the_north_bathroom_only(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    out = chat.turn("north")
    assert set(chat.written) == NORTH
    assert out.answer == "Turned on the north bathroom lights."
    assert "awaiting_room_choice" not in chat.stored_parameters()


@pytest.mark.parametrize("reply", ["north.", "North!", "the north one", "um the north bathroom please"])
def test_speech_to_text_noise_around_the_answer_still_completes_it(monkeypatch, reply):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    chat.turn(reply)
    assert set(chat.written) == NORTH, reply


def test_the_reply_is_classified_as_control_without_a_model_call(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    prev = chat.store["s1"]
    from orchestrator.room_directory import current_directory

    pending = prev["parameters"]["awaiting_room_choice"]
    assert room_choice.reply_fits(pending, "north", current_directory(), is_question=False)
    assert not room_choice.reply_fits(pending, "north", current_directory(), is_question=True)
    assert not room_choice.reply_fits(pending, "purple", current_directory(), is_question=False)
    assert chat.llm.calls == 0


# ---------------------------------------------------------------------------
# Chained pendings: the stored words are replayed, never re-decided
# ---------------------------------------------------------------------------

def test_the_chain_bathroom_north_gate_prompt_yes_writes_the_north_bathroom(monkeypatch):
    chat = Chat(monkeypatch, threshold=1)
    ask_bathroom(chat)
    prompt = chat.turn("north")
    assert chat.written == [] and prompt.answer.endswith("?"), prompt.answer
    assert "north bathroom" in prompt.answer and "_" not in prompt.answer
    params = chat.stored_parameters()
    assert "pending_write_confirmation" in params and "awaiting_room_choice" not in params
    assert chat.store["s1"]["query"] == "turn on the bathroom lights", "the first-turn words are what replays"
    chat.turn("yes")
    assert set(chat.written) == NORTH


def test_turn_on_the_lights_over_the_threshold_then_yes_writes_the_speakers_room(monkeypatch):
    chat = Chat(monkeypatch, threshold=1)
    first = chat.turn("the lights please")
    assert chat.written == [] and first.answer.endswith("?")
    chat.turn("yes", room="galley")
    assert set(chat.written) == STUDY, "decided once, in the study, whatever room the replay carries"


# ---------------------------------------------------------------------------
# Strict reading of the answer
# ---------------------------------------------------------------------------

def test_a_reply_that_fits_two_candidates_is_asked_again_once_then_dropped(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    second = chat.turn("bathroom")
    assert second.answer == QUESTION and chat.written == []
    pending = chat.stored_parameters()["awaiting_room_choice"]
    assert pending["reasked"] is True
    first_nonce = pending["nonce"]
    third = chat.turn("bathroom")
    assert chat.written == [] and "awaiting_room_choice" not in chat.stored_parameters()
    assert first_nonce not in route_control._NONCE_FALLBACK, "never claimed"


def test_a_question_cancels_the_pending_and_is_answered_as_a_question(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    nonce = chat.stored_parameters()["awaiting_room_choice"]["nonce"]
    out = chat.turn("is the north bathroom light on")
    assert chat.written == []
    assert "awaiting_room_choice" not in chat.stored_parameters()
    assert nonce not in route_control._NONCE_FALLBACK, "the nonce was not claimed by a question"
    assert "lights are on" in out.answer or "No lights" in out.answer or "light is on" in out.answer


def test_a_command_reply_cancels_the_pending_and_runs_fresh(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    chat.turn("turn on the study lights")
    assert set(chat.written) == STUDY
    assert "awaiting_room_choice" not in chat.stored_parameters()


def test_a_reply_naming_a_command_verb_with_the_room_is_not_an_answer(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    nonce = chat.stored_parameters()["awaiting_room_choice"]["nonce"]
    assert room_choice.read_reply(
        chat.stored_parameters()["awaiting_room_choice"], "turn on the north",
        __import__("orchestrator.room_directory", fromlist=["x"]).current_directory(),
    ).kind == "none"
    chat.turn("turn on the north")
    assert "awaiting_room_choice" not in chat.stored_parameters()
    assert nonce not in route_control._NONCE_FALLBACK, "a command is not an answer: the nonce stays unclaimed"


def test_never_mind_declines(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    out = chat.turn("never mind")
    assert out.answer == DECLINED_TEXT and chat.written == []


def test_an_expired_pending_runs_the_reply_fresh(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    chat.clock[0] += room_choice.TTL + 1
    chat.turn("north")
    assert not (set(chat.written) & NORTH)
    assert "awaiting_room_choice" not in chat.stored_parameters()


# ---------------------------------------------------------------------------
# Identity: another caller never touches the pending
# ---------------------------------------------------------------------------

def test_a_foreign_callers_north_writes_nothing_and_leaves_the_pending_intact(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    before = json.dumps(chat.store["s1"], sort_keys=True)
    stores_before = len(chat.stores)
    chat.turn("north", fp="fp-intruder")
    assert not (set(chat.written) & NORTH)
    assert len(chat.stores) == stores_before, "a foreign turn stores nothing"
    assert json.dumps(chat.store["s1"], sort_keys=True) == before
    chat.turn("north")
    assert set(chat.written) == NORTH, "the real caller can still answer"


def test_a_foreign_callers_state_question_does_not_overwrite_the_pending(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    before = json.dumps(chat.store["s1"], sort_keys=True)
    chat.turn("are the study lights on", fp="fp-intruder")
    chat.turn("are the bathroom lights on", fp="fp-intruder")
    assert json.dumps(chat.store["s1"], sort_keys=True) == before


def test_the_same_caller_in_a_different_session_has_no_pending_to_answer(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    chat.turn("north", session="s2")
    assert not (set(chat.written) & NORTH)


def test_a_registry_change_between_question_and_reply_changes_the_fingerprint_and_nothing_writes(monkeypatch):
    """R6: the room is part of the fingerprint, so a reply carrying another
    fingerprint is another caller's: no write, pending intact."""
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    chat.turn("north", fp="fp-after-registry-change")
    assert chat.written == [] and "awaiting_room_choice" in chat.stored_parameters()


def test_a_changed_fingerprint_on_the_yes_after_a_gate_prompt_is_refused(monkeypatch):
    chat = Chat(monkeypatch, threshold=1)
    ask_bathroom(chat)
    chat.turn("north")
    out = chat.turn("yes", fp="fp-after-registry-change")
    assert out.answer == route_control.CROSS_IDENTITY_TEXT and chat.written == []


# ---------------------------------------------------------------------------
# Authorize before asking
# ---------------------------------------------------------------------------

RESTRICT_SOUTH = r"^light\.south_lamp"


def test_a_caller_denied_one_candidate_is_not_asked_about_it(monkeypatch):
    chat = Chat(monkeypatch)
    perms = guest(allowed_intents=["control"], restricted_entities=[RESTRICT_SOUTH])
    out = chat.turn("turn on the bathroom lights", perms=perms, mode="guest")
    assert "south" not in out.answer.lower()
    assert out.answer == "Which bathroom: lower or north?"
    assert chat.written == []


def test_a_caller_denied_every_candidate_gets_the_refusal_and_no_room_list(monkeypatch):
    chat = Chat(monkeypatch)
    perms = guest(allowed_intents=["control"], restricted_entities=[r"^light\.(north|south|lower)_lamp"])
    out = chat.turn("turn on the bathroom lights", perms=perms, mode="guest")
    assert chat.written == []
    for name in ("north", "south", "lower"):
        assert name not in out.answer.lower()
    assert "awaiting_room_choice" not in chat.stored_parameters()


def test_a_flip_from_owner_to_guest_between_turns_reauthorizes_the_reply(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    perms = guest(allowed_intents=["control"], restricted_entities=[r"^light\.north_lamp"])
    out = chat.turn("north", perms=perms, mode="guest")
    assert chat.written == [] and "guest mode" in out.answer.lower()


def test_which_room_reply_naming_a_room_the_guest_scope_denies_is_refused(monkeypatch):
    chat = Chat(monkeypatch)
    first = chat.turn("turn on the lights", room="unknown")
    assert first.answer == "Which room?"
    assert chat.stored_parameters()["awaiting_room_choice"]["kind"] == "room"
    perms = guest(allowed_intents=["control"], restricted_entities=[RESTRICT_SOUTH])
    out = chat.turn("south bathroom", perms=perms, mode="guest")
    assert chat.written == [] and "guest mode" in out.answer.lower()


def test_which_room_reply_completes_for_the_owner(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("turn on the lights", room="unknown")
    chat.turn("the galley")
    assert chat.written and all(w.startswith("light.galley_lamp") for w in chat.written)


def test_the_candidates_are_the_stored_ones_even_if_the_registry_changes_before_the_reply(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    from shared import ha_registry as hr

    trimmed = {**chat.raw_registry,
               "areas": [a for a in chat.raw_registry["areas"] if a["area_id"] != "area_south"],
               "entities": [e for e in chat.raw_registry["entities"] if "south" not in e["entity_id"]]}
    chat.rig.registry._snapshot = hr.RegistrySnapshot.from_raw(trimmed, fetched_at=3.0)
    chat.turn("south")
    assert set(chat.written) <= SOUTH, "the stored candidate list decided the room, not the new snapshot"


# ---------------------------------------------------------------------------
# The nonce
# ---------------------------------------------------------------------------

def test_two_replies_racing_write_exactly_once(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)
    outs = chat.gather(("north", {}), ("north", {}))
    assert sorted(set(chat.written)) == sorted(NORTH)
    assert len(chat.written) == len(NORTH), "one write, not two"
    answers = sorted(o.answer for o in outs)
    assert REPLY_AGAIN_TEXT in answers


def test_with_redis_configured_a_claim_that_errors_writes_nothing(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat)

    class BrokenRedis:
        async def set(self, *a, **k):
            raise ConnectionError("down")

    class Cache:
        client = BrokenRedis()

    _runtime.set_cache_client(Cache())
    out = chat.turn("north")
    assert chat.written == [] and out.answer == REPLY_AGAIN_TEXT


# ---------------------------------------------------------------------------
# What the surface can carry
# ---------------------------------------------------------------------------

def test_without_follow_up_support_the_question_is_asked_and_nothing_is_stored(monkeypatch):
    chat = Chat(monkeypatch)
    out = chat.turn("turn on the bathroom lights", supports_followup=False)
    assert out.answer == QUESTION
    assert "awaiting_room_choice" not in chat.stored_parameters()


def test_the_fast_path_defers_on_a_held_room_choice():
    assert fast_path._context_reason({"awaiting_room_choice": {"nonce": "n"}}) == fast_path.REASON_AWAITING_CONTEXT


def test_a_foreign_pending_of_either_kind_counts_as_foreign():
    own = {"awaiting_room_choice": {"fingerprint": "me"}}
    assert helpers.holds_foreign_pending({"parameters": own}, "me") is False
    assert helpers.holds_foreign_pending({"parameters": own}, "someone-else") is True
    assert helpers.holds_foreign_pending({"parameters": {"awaiting_room_choice": {"nonce": "n"}}}, "me") is True
    confirmation = {"pending_write_confirmation": {"fingerprint": "me"}}
    assert helpers.holds_foreign_pending({"parameters": confirmation}, "someone-else") is True
    assert helpers.holds_foreign_pending({"parameters": {}}, "me") is False


# ---------------------------------------------------------------------------
# Read path and exclusivity
# ---------------------------------------------------------------------------

def test_a_state_question_with_an_ambiguous_room_asks_through_the_state_question_seam(monkeypatch):
    chat = Chat(monkeypatch)
    out = chat.turn("are the bathroom lights on")
    assert out.answer == QUESTION
    params = chat.stored_parameters()
    assert params.get("awaiting_state_question") is True and "awaiting_room_choice" not in params
    assert sorted(c[0] for c in params["room_candidates"]) == ["lower_bathroom", "north_bathroom", "south_bathroom"]
    assert chat.written == []


def test_the_answer_to_a_read_path_question_answers_the_question_for_that_room(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("are the bathroom lights on")
    out = chat.turn("north")
    assert chat.written == []
    assert "north bathroom" in out.answer, out.answer
    assert "2 lights" in out.answer


def test_a_room_choice_context_never_holds_a_state_question_key_and_vice_versa(monkeypatch):
    chat = Chat(monkeypatch)
    ask_bathroom(chat, session="a")
    chat.turn("are the bathroom lights on", session="b")
    assert "awaiting_state_question" not in chat.stored_parameters("a")
    assert "awaiting_room_choice" not in chat.stored_parameters("b")


def test_a_control_answer_is_never_written_to_the_semantic_cache():
    text = (Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "main.py").read_text()
    assert 'and response.intent != IntentCategory.CONTROL.value' in text


# ---------------------------------------------------------------------------
# Store sites
# ---------------------------------------------------------------------------

SITE_REASONS = {
    "_await_state_question_clarification": "returns before storing when foreign_pending",
    "_clear_pending": "runs only after the identity check, for the pending's own caller",
    "_clear_room_choice": "runs only after the identity check, for the pending's own caller",
    "_store_pending_write_confirmation": "reached only from paths that passed the carry-pending check",
    "_store_room_choice": "reached only when carry_pending (which requires not foreign_pending)",
    "_execute_and_record": "success store is guarded by `not foreign_pending`",
    "_resolve_pending_write_confirmation": "the pending's own caller's replay, after the identity check",
    "_resolve_pending_room_choice": "the pending's own caller, after the identity check",
    "route_control_node": "state-question success store is guarded by `not foreign_pending`",
}


def store_sites(source: str):
    tree = ast.parse(source)
    sites = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "store_conversation_context":
                sites.append(func.name)
    return sites


def unguarded(source: str):
    """Store sites whose function is not in SITE_REASONS."""
    return sorted({name for name in store_sites(source) if name not in SITE_REASONS})


def test_store_sites_respect_foreign_pending():
    source = (Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "nodes" / "route_control.py").read_text()
    sites = store_sites(source)
    assert len(sites) >= 5, "population floor"
    assert unguarded(source) == []
    tree = ast.parse(source)
    guarded_by_text = {
        "_await_state_question_clarification": "foreign_pending",
        "_execute_and_record": "not foreign_pending",
    }
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and func.name in guarded_by_text:
            assert guarded_by_text[func.name] in ast.get_source_segment(source, func), func.name


def test_a_planted_unguarded_store_site_fails_the_check():
    planted = "async def sneaky(state):\n    await store_conversation_context(session_id='s')\n"
    assert unguarded(planted) == ["sneaky"]

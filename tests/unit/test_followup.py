"""Follow-ups keep their subject (orchestrator/followup.py and its readers).

`open_task` says whether the last exchange left a question or an offer open on
an information intent; `resolve` says what a bare reply does with it. Every
guard that must return None has a row, and each None row has a positive control
(the same words resolve once the guard is lifted), so a guard that never fires
and a resolver that never resolves both fail.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest

from . import _public_audience_harness as h  # noqa: F401  (stubs the heavy imports first)
from orchestrator import followup
from orchestrator.fast_path import ends_with_promise
from orchestrator.state import IntentCategory
from orchestrator.utterance_kind import classify_utterance

NOW = 1_800_000_000.0
REQUEST = "Any good Italian places nearby?"
QUESTION = "I found three places. Want me to look for outdoor seating?"
PROMISE = "I found three places. I can also check for outdoor seating if you'd like."


def exchange(user=REQUEST, assistant=QUESTION, age=30.0, session_class="other"):
    return {"user": user, "assistant": assistant, "at": NOW - age, "session_class": session_class}


def task_for(last=None, prev_intent="dining", prev_query=None, session_class="other"):
    return followup.open_task(last or exchange(), prev_intent, prev_query, session_class=session_class, now=NOW)


def resolve(query, task=None, prev_context=None):
    return followup.resolve(query, task or task_for(), classify_utterance(query), prev_context)


# --- open_task ---------------------------------------------------------------------------------------

NEVER = [
    IntentCategory.CONTROL, IntentCategory.MUSIC_PLAY, IntentCategory.MUSIC_CONTROL, IntentCategory.TV_CONTROL,
    IntentCategory.TEXT_ME_THAT, IntentCategory.NOTIFICATION_PREF, IntentCategory.UNKNOWN, IntentCategory.TESLA,
]


def test_the_information_intents_are_a_counted_set_with_named_members():
    assert len(followup.INFO_INTENTS) >= 10
    assert {"dining", "weather", "sports", "general_info", "recipes"} <= followup.INFO_INTENTS
    assert not followup.INFO_INTENTS & {i.value for i in NEVER}


@pytest.mark.parametrize("intent", sorted(followup.INFO_INTENTS))
def test_an_information_intent_with_an_open_question_has_a_task(intent):
    task = task_for(prev_intent=intent)
    assert task is not None and task.intent == intent and task.kind == "question"


@pytest.mark.parametrize("intent", [i.value for i in NEVER] + [None, "music", "not_an_intent"])
def test_open_task_is_never_created_for_control_music_tv_sms_notifications_or_a_non_intent(intent):
    assert task_for(prev_intent=intent) is None


def test_a_promise_opens_a_task_too():
    assert task_for(exchange(assistant=PROMISE)).kind == "promise"


@pytest.mark.parametrize("assistant", [
    "It is sunny and mild today.",
    "I found three places.",
    "I'm not sure how to help with that. Could you rephrase your question?",
    "I couldn't look that up right now.",
    "I'm not confident in my response. Could you please rephrase your question?",
], ids=["statement", "found", "fallback-1", "decline", "fallback-2"])
def test_a_turn_that_asked_nothing_or_is_the_pipelines_own_fallback_opens_no_task(assistant):
    assert task_for(exchange(assistant=assistant)) is None


def test_a_stale_exchange_opens_no_task_and_a_fresh_one_does():
    assert task_for(exchange(age=599)) is not None
    assert task_for(exchange(age=601)) is None


def test_an_exchange_with_no_timestamp_opens_no_task():
    last = exchange()
    last["at"] = None
    assert task_for(last) is None


def test_an_exchange_from_another_session_class_opens_no_task():
    assert task_for(exchange(session_class="guest"), session_class="other") is None
    assert task_for(exchange(session_class="guest"), session_class="guest") is not None


def test_no_exchange_opens_no_task():
    assert followup.open_task({}, "dining", session_class="other", now=NOW) is None
    assert followup.open_task(None, "dining", session_class="other", now=NOW) is None


def test_a_reply_turn_carries_the_request_it_resumed():
    resolved = "Any good Italian places nearby. You asked: Want me to look for outdoor seating? The user answered: yes."
    last = exchange(user="yes", assistant="Here is one. Want directions?")
    assert task_for(last, prev_query=resolved).request == resolved
    assert task_for(last, prev_query="something else entirely").request == "yes"


# --- resolve -----------------------------------------------------------------------------------------

AFFIRMATIONS = ["yes", "Yes.", "YES!", "yes!", "yeah, sure.", "sure.", "okay", "yes please", "go ahead", "do it", "Yes?"]
DECLINES = ["no thanks.", "no, thanks.", "nah", "No.", "nope"]
SLOTS = ["in the north district", "downtown", "cheap ones", "something with a patio"]


@pytest.mark.parametrize("query", AFFIRMATIONS, ids=AFFIRMATIONS)
def test_an_affirmation_resumes_the_original_request(query):
    result = resolve(query)
    assert isinstance(result, followup.Affirm)
    assert "Italian places nearby" in result.resolved_request and "outdoor seating" in result.resolved_request


@pytest.mark.parametrize("query", DECLINES, ids=DECLINES)
def test_a_decline_ends_the_topic(query):
    assert isinstance(resolve(query), followup.Decline)


@pytest.mark.parametrize("query", SLOTS, ids=SLOTS)
def test_a_short_answer_fills_the_blank(query):
    result = resolve(query)
    assert isinstance(result, followup.Slot)
    assert result.resolved_request.startswith("Any good Italian places nearby") and query in result.resolved_request


def test_the_reply_forms_are_counted_with_named_members():
    assert len(AFFIRMATIONS) >= 8 and len(DECLINES) >= 4 and len(SLOTS) >= 3
    assert {"Yes.", "YES!", "yeah, sure.", "no, thanks."} <= set(AFFIRMATIONS + DECLINES)


def test_yes_and_Yes_resolve_to_the_same_request():
    assert resolve("yes") == resolve("Yes.") == resolve("YES!")


def test_a_slot_needs_a_question_not_a_promise():
    promise = task_for(exchange(assistant=PROMISE))
    assert isinstance(resolve("in the north district", promise), type(None))
    assert isinstance(resolve("yes", promise), followup.Affirm)  # a promise is answered by yes


GUARDS = {
    "a command": "turn off the kitchen lights",
    "a command with a polite word": "please turn on the porch light",
    "words that name a device": "the kitchen lights",
    "a state question": "is the garage door open",
    "a question": "what about tomorrow",
    "a question with a mark": "any vegan ones?",
    "more than six words": "in the district where the old train station used to be",
}


@pytest.mark.parametrize("query", GUARDS.values(), ids=GUARDS.keys())
def test_words_that_are_something_else_resolve_to_nothing(query):
    assert resolve(query) is None


def test_a_reply_that_classifies_as_a_command_is_never_resumed_whatever_the_vocabulary():
    """The guard holds on the classification, not on today's word lists."""
    imperative = SimpleNamespace(kind=followup.UtteranceKind.IMPERATIVE)
    assert followup.resolve("yes", task_for(), imperative) is None
    assert isinstance(followup.resolve("yes", task_for(), SimpleNamespace(kind=followup.UtteranceKind.UNKNOWN)), followup.Affirm)


def test_a_reply_that_names_a_device_is_never_resumed_whatever_the_vocabulary(monkeypatch):
    monkeypatch.setattr(followup.access_target, "names_device", lambda text: True)
    assert followup.resolve("yes", task_for(), classify_utterance("yes")) is None
    monkeypatch.setattr(followup.access_target, "names_device", lambda text: False)
    assert isinstance(followup.resolve("yes", task_for(), classify_utterance("yes")), followup.Affirm)


@pytest.mark.parametrize("pending", ["pending_write_confirmation", "awaiting_room_choice", "awaiting_state_question"])
def test_an_ha_pending_owns_yes_and_no(pending):
    context = {"intent": "dining", "parameters": {pending: {"nonce": "n"}}}
    assert resolve("yes", prev_context=context) is None
    assert resolve("no thanks", prev_context=context) is None
    assert isinstance(resolve("yes", prev_context={"intent": "dining", "parameters": {pending: None}}), followup.Affirm)


def test_an_unknown_awaiting_key_also_owns_the_reply():
    assert resolve("yes", prev_context={"parameters": {"awaiting_something_new": {"x": 1}}}) is None


def test_with_no_task_nothing_resolves():
    assert followup.resolve("yes", None, classify_utterance("yes")) is None


@pytest.mark.parametrize("word", ["yes", "no"])
def test_yes_after_a_non_question_turn_is_not_a_resume(word):
    assert task_for(exchange(assistant="It is sunny today.")) is None
    assert followup.resolve(word, task_for(exchange(assistant="It is sunny today.")), classify_utterance(word)) is None


# --- the cache asks the same question before the graph runs --------------------------------------------

def slot_candidate(query, last, session_class="other"):
    return followup.slot_candidate(query, last, classify_utterance(query), session_class=session_class, now=NOW)


def test_a_short_answer_to_a_question_is_a_slot_candidate():
    assert slot_candidate("in the north district", exchange())
    assert not slot_candidate("in the north district", exchange(assistant="It is sunny today."))
    assert not slot_candidate("in the north district", exchange(assistant=PROMISE))
    assert not slot_candidate("in the north district", exchange(age=900))
    assert not slot_candidate("turn off the kitchen lights", exchange())


# --- the exchange is read from the session the entry point already loaded ------------------------------

def session(*messages, session_id="s1"):
    return SimpleNamespace(session_id=session_id, messages=list(messages))


def message(role, content, timestamp="2027-01-15T08:00:00", **metadata):
    return {"role": role, "content": content, "timestamp": timestamp, "metadata": metadata}


def test_the_last_exchange_is_the_last_user_message_and_the_reply_to_it():
    found = followup.last_exchange_from_session(session(
        message("user", "old question"), message("assistant", "old answer"),
        message("user", REQUEST), message("assistant", QUESTION, "2027-01-15T08:00:05"),
    ))
    assert found["user"] == REQUEST and found["assistant"] == QUESTION
    assert found["session_class"] == "other"
    assert found["at"] == pytest.approx(1800000005.0)


def test_a_session_with_no_assistant_message_or_a_fast_path_reply_has_no_exchange():
    assert followup.last_exchange_from_session(session(message("user", "hi"))) is None
    assert followup.last_exchange_from_session(session()) is None
    assert followup.last_exchange_from_session(session(
        message("user", "hello"), message("assistant", "Hello. How can I help?", fast_path="greeting")
    )) is None


def test_the_session_class_comes_from_the_id_prefix():
    owner = followup.last_exchange_from_session(session(
        message("user", REQUEST), message("assistant", QUESTION), session_id="own-abc"))
    assert owner["session_class"] == "owner"


# --- the promise predicate the fast path and the task share --------------------------------------------

@pytest.mark.parametrize("text", [
    PROMISE, "Want me to check the standings?", "Let me know if you want the forecast for tomorrow.",
    "I could also look up nearby parking.", "I'll gladly pull up the menu.", "Would you like directions?",
])
def test_an_offer_is_a_promise(text):
    assert ends_with_promise(text)


@pytest.mark.parametrize("text", [
    "It is sunny today.", "The Lakers won 110 to 98.", "Playing jazz in the kitchen.", "", None,
])
def test_a_plain_statement_is_no_promise(text):
    assert not ends_with_promise(text)


# --- the summary keeps the assistant's last question -----------------------------------------------------

def _summarize(history, previous_summary=""):
    from orchestrator import helpers

    llm = h.CapturingLLM(content="The user asked about Italian places nearby.")
    h._runtime.set_llm_router(llm)
    with mock.patch.object(helpers, "get_component_config", mock.AsyncMock(return_value={"model_name": "m"})):
        result = asyncio.run(helpers.summarize_conversation_history(history, "yes", previous_summary=previous_summary))
    return result, llm


SHORT = [{"role": "user", "content": REQUEST}, {"role": "assistant", "content": QUESTION}]
LONG = [
    {"role": "user", "content": "I live near the park"},
    {"role": "assistant", "content": "Nice spot."},
    {"role": "user", "content": REQUEST},
    {"role": "assistant", "content": QUESTION},
]


def test_the_short_path_keeps_the_assistants_last_reply():
    result, llm = _summarize(SHORT)
    assert REQUEST in result and "Assistant's last reply: " + QUESTION in result
    assert llm.captured == []  # still no model call for a short history


def test_the_long_path_keeps_the_assistants_last_reply_and_asks_the_model_to_keep_open_questions():
    result, llm = _summarize(LONG)
    assert result.startswith("Previous context: The user asked about Italian places")
    assert result.endswith("Assistant's last reply: " + QUESTION)
    assert "unanswered" in llm.text() or "has not answered" in llm.text()


def test_a_failed_summary_still_keeps_the_last_reply():
    from orchestrator import helpers

    h._runtime.set_llm_router(h.CapturingLLM(content=""))
    with mock.patch.object(helpers, "get_component_config", mock.AsyncMock(return_value={"model_name": "m"})):
        result = asyncio.run(helpers.summarize_conversation_history(LONG, "yes"))
    assert result == "Previous context: Assistant's last reply: " + QUESTION


def test_only_the_newest_reply_rides_along_when_summaries_accumulate():
    first, _ = _summarize(SHORT)
    follow_up = [{"role": "user", "content": "yes"}, {"role": "assistant", "content": "Here is one. Want directions?"}]
    short, _ = _summarize(follow_up, previous_summary=first)
    long, _ = _summarize(SHORT + follow_up, previous_summary=first)
    for second in (short, long):
        assert second.count("Assistant's last reply:") == 1
        assert second.endswith("Assistant's last reply: Here is one. Want directions?")
    assert QUESTION not in short  # the earlier reply is gone from the accumulated text


def test_a_long_last_reply_keeps_its_ending_where_the_question_is():
    long_reply = "Filler sentence. " * 60 + "Want me to look for outdoor seating?"
    result, _ = _summarize([{"role": "user", "content": REQUEST}, {"role": "assistant", "content": long_reply}])
    assert result.endswith("Want me to look for outdoor seating?")
    assert len(result.split("Assistant's last reply: ")[1]) <= 310

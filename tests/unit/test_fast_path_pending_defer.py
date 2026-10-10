"""The fast path never answers a reply to an open question.

For every acknowledgement, thanks and farewell word, against every kind of
open question -- a real pending fan-out confirmation stored by
`route_control._store_pending_write_confirmation`, an `awaiting_state_question`
context from `_await_state_question_clarification`, a last assistant message
ending in "?", and an unreadable context store -- the turn reaches the full
pipeline, the stored context is left untouched, and a following "yes" from the
same caller still replays the confirmation. Positive controls prove the same
words are answered without a question open.
"""
from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest

from shared import fast_path_vocab as vocab

from . import _public_audience_harness as h
from . import _fast_path_harness as fp

DEFER_KINDS = {"ack", "thanks", "farewell"}
WORDS = sorted(k for k, (kind, _) in vocab.REPLIES.items() if kind in DEFER_KINDS)
CONFIRM_PROMPT = "That would turn off 2 lights in the office. Should I go ahead?"
FINGERPRINT = None  # computed in a test: write_fanout.caller_fingerprint(...)


def test_word_population_floor_and_named_members():
    assert len(WORDS) >= 15
    assert {"thanks", "thank you", "got it", "never mind", "thats all", "bye", "see you"} <= set(WORDS)


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def _run(coro):
    return asyncio.run(coro)


def _fingerprint():
    from orchestrator import write_fanout

    return write_fanout.caller_fingerprint("household", "voice-a", "office", "owner")


async def _open_pending(rig, session_id, *, fingerprint=None):
    from orchestrator import write_fanout
    from orchestrator.nodes import route_control as rc
    from orchestrator.state import OrchestratorState

    state = OrchestratorState(query="turn off the office lights", session_id=session_id)
    state.caller_fingerprint = fingerprint or _fingerprint()
    block = write_fanout.FanoutBlock(
        writes=(write_fanout.PlannedWrite("light", "turn_off", ("light.office_1", "light.office_2")),),
        unbounded=False,
    )
    intent = {"device_type": "light", "room": "office", "action": "turn_off", "target_scope": "group", "parameters": {}}
    await rc._store_pending_write_confirmation(state, intent, CONFIRM_PROMPT, block)


async def _open_awaiting(rig, session_id):
    from orchestrator.nodes import route_control as rc
    from orchestrator.state import OrchestratorState

    state = OrchestratorState(query="is it on", session_id=session_id)
    await rc._await_state_question_clarification(state, "office", foreign_pending=False)


async def _open_question(rig, session_id):
    sm = h._runtime.get_session_manager()
    await sm.get_or_create_session(session_id=session_id, caller_class="other")
    await sm.add_message(session_id, "user", "turn on a light")
    await sm.add_message(session_id, "assistant", "Which light do you mean?")


async def _open_foreign_pending(rig, session_id):
    await _open_pending(rig, session_id, fingerprint="someone-else-entirely")


OPENERS = {
    "pending_confirmation": _open_pending,
    "awaiting_state_question": _open_awaiting,
    "assistant_question": _open_question,
    "foreign_pending": _open_foreign_pending,
}


def _context_key(session_id):
    from orchestrator.session_keys import context_storage_key

    return context_storage_key(session_id)


async def _deferred_turn(monkeypatch_rig, route, word, opener, *, session_id=None):
    sid = session_id or fp.session_id_for(route)
    await OPENERS[opener](monkeypatch_rig, sid)
    before = dict(monkeypatch_rig.cache.client.data)
    resp = await fp.send(route, word)
    return resp, before, dict(monkeypatch_rig.cache.client.data)


@pytest.mark.parametrize("opener", list(OPENERS))
@pytest.mark.parametrize("word", WORDS)
def test_every_word_defers_inside_every_kind_of_open_question(monkeypatch, word, opener):
    rig = fp.install(monkeypatch, slow="record")
    resp, before, after = _run(_deferred_turn(rig, "query", word, opener))
    assert rig.reached == ["graph"], f"{word!r} was answered inside {opener}"
    assert fp.answer_of("query", resp) == fp.SLOW_ANSWER
    key = _context_key(fp.session_id_for("query"))
    assert after.get(key) == before.get(key), "the stored context must be untouched"


@pytest.mark.parametrize("route", fp.ROUTES)
@pytest.mark.parametrize("opener", list(OPENERS))
def test_every_route_defers(monkeypatch, route, opener):
    rig = fp.install(monkeypatch, slow="record")
    resp, before, after = _run(_deferred_turn(rig, route, "thanks", opener))
    assert rig.reached, f"{route} answered inside {opener}"
    assert fp.answer_of(route, resp) == fp.SLOW_ANSWER
    key = _context_key(fp.session_id_for(route))
    assert after.get(key) == before.get(key)


def test_a_context_read_error_defers(monkeypatch):
    rig = fp.install(monkeypatch, slow="record")
    rig.cache.client.fail_reads = True
    counter = mock.MagicMock()
    monkeypatch.setattr(h.main, "fast_path_deferred_total", counter)
    resp = _run(fp.send("query", "thanks"))
    assert rig.reached == ["graph"]
    assert fp.answer_of("query", resp) == fp.SLOW_ANSWER
    counter.labels.assert_called_once_with(route="query", reason="context_unreadable")


@pytest.mark.parametrize("opener,reason", [
    ("pending_confirmation", "pending_confirmation"),
    ("foreign_pending", "pending_confirmation"),
    ("awaiting_state_question", "awaiting_context"),
    ("assistant_question", "open_question"),
])
def test_the_deferral_reason_is_counted(monkeypatch, opener, reason):
    rig = fp.install(monkeypatch, slow="record")
    counter = mock.MagicMock()
    monkeypatch.setattr(h.main, "fast_path_deferred_total", counter)
    _run(_deferred_turn(rig, "query", "thanks", opener))
    counter.labels.assert_called_once_with(route="query", reason=reason)


def test_a_yes_after_the_deferred_turn_still_replays_the_confirmation(monkeypatch):
    from orchestrator import write_fanout
    from orchestrator.mode_permission import ha_permission_scope
    from orchestrator.nodes import route_control as rc
    from orchestrator.state import OrchestratorState

    rig = fp.install(monkeypatch, slow="record")
    sid = fp.session_id_for("query")

    async def scenario():
        await _open_pending(rig, sid)
        first = await fp.send("query", "never mind")          # deferred, not consumed
        stored = json.loads(rig.cache.client.data[_context_key(sid)])
        controller = mock.MagicMock()
        controller.execute_intent = mock.AsyncMock(return_value="Done. I turned them off.")
        h._runtime.set_smart_controller(controller)
        h._runtime.set_ha_client(mock.MagicMock())
        state = OrchestratorState(query="yes", session_id=sid, room="office")
        state.prev_context = stored
        state.caller_fingerprint = _fingerprint()
        perms = {"mode": "owner"}
        with ha_permission_scope(perms, mode="owner", session_id=sid) as scope:
            handled = await rc._resolve_pending_write_confirmation(state, scope)
        return first, handled, state, controller

    first, handled, state, controller = _run(scenario())
    assert fp.answer_of("query", first) == fp.SLOW_ANSWER
    assert handled is True
    assert state.answer == "Done. I turned them off."
    controller.execute_intent.assert_awaited_once()
    assert controller.execute_intent.await_args.args[0]["action"] == "turn_off"
    assert write_fanout.BARE_AFFIRMATION_RE.match(write_fanout.normalize_reply("yes"))


# --- positive controls: the same words ARE answered with no question open --------------


@pytest.mark.parametrize("word", WORDS)
def test_every_word_is_answered_when_nothing_is_open(monkeypatch, word):
    rig = fp.install(monkeypatch)
    answer = fp.answer_of("query", _run(fp.send("query", word)))
    kind, text = vocab.REPLIES[word]
    assert answer == text
    assert rig.reached == []


def test_an_unrelated_stored_context_does_not_defer(monkeypatch):
    """A control context with no pending/awaiting key is not an open question."""
    from orchestrator.helpers import store_conversation_context

    rig = fp.install(monkeypatch)

    async def scenario():
        await store_conversation_context(
            session_id=fp.session_id_for("query"), intent="control", query="turn on the lamp",
            entities={"room": "office"}, parameters={"action": "turn_on", "room": "office"}, response="Done.",
        )
        return fp.answer_of("query", await fp.send("query", "thanks"))

    assert _run(scenario()) == "You're welcome."
    assert rig.reached == []


def test_an_expired_memory_context_does_not_defer(monkeypatch):
    from orchestrator.fast_path import fast_path_open_question
    from orchestrator.state import ConversationContext

    fp.install(monkeypatch)
    ctx = ConversationContext(intent="control", query="q", parameters={"pending_write_confirmation": {"n": 1}})
    h._runtime.get_memory_context()["s1"] = {"context": ctx, "expires_at": 0}
    assert _run(fast_path_open_question("s1", None)) is None
    h._runtime.get_memory_context()["s1"] = {"context": ctx, "expires_at": 4_102_444_800}
    assert _run(fast_path_open_question("s1", None)) == "pending_confirmation"


def test_a_fast_path_greeting_does_not_make_the_next_turn_defer(monkeypatch):
    """"Hello. How can I help?" ends in "?" but is the fast path's own reply."""
    rig = fp.install(monkeypatch)

    async def scenario():
        first = fp.answer_of("query", await fp.send("query", "hello"))
        await h._runtime.drain_background()
        second = fp.answer_of("query", await fp.send("query", "what time is it", interface_type="text"))
        return first, second

    first, second = _run(scenario())
    assert first == "Hello. How can I help?"
    assert second.startswith("It's ")
    assert rig.reached == []


@pytest.mark.parametrize("text,expected", [
    ("Which light do you mean?", True),
    ('Did you say "the lamp?"', True),
    ("Did you mean (the lamp?)", True),
    ("Which light?  ", True),
    ("Which light？", True),
    ("Done.", False),
    ("What? Never mind.", False),
    ("", False),
    (None, False),
])
def test_question_detection_boundaries(text, expected):
    from orchestrator.fast_path import _ends_with_question

    assert _ends_with_question(text) is expected


def test_last_assistant_text_ignores_the_fast_paths_own_message():
    from orchestrator.fast_path import last_assistant_text

    session = mock.MagicMock()
    session.messages = [
        {"role": "assistant", "content": "Which light?", "metadata": {}},
        {"role": "user", "content": "hello", "metadata": {}},
        {"role": "assistant", "content": "Hello. How can I help?", "metadata": {"fast_path": "greeting"}},
    ]
    assert last_assistant_text(session) is None
    session.messages[-1]["metadata"] = {}
    assert last_assistant_text(session) == "Hello. How can I help?"
    session.messages = []
    assert last_assistant_text(session) is None

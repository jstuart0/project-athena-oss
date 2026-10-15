"""Replay of the live-conversation audit's problem shapes through the pure functions.

Every row is a synthetic paraphrase of a shape the audit described (a statement
read as a command, a bare "yes" with no subject, an unknown speaker asked who
they are, markdown spoken aloud, a bare 502, a made-up venue, a live game called
a result). No row carries real conversation text, a name, a room or an address.

Table-driven: one pytest case per row. Each row imports the function it checks
inside its own lambda, so a tree that lacks a module (or a keyword) fails that
row alone and the rest still report. Each family has a population floor and one
named member whose value before the campaign was different.
"""
from __future__ import annotations

import asyncio
import importlib
import socket
from types import SimpleNamespace as NS

import pytest

from . import _public_audience_harness as h  # noqa: F401  (stubs the heavy imports and the env first)


def fn(module: str, name: str):
    """Import at call time: the row, not the module, fails on a tree without it."""
    return getattr(importlib.import_module(module), name)


# --- helpers a row calls (each imports what it uses) ---------------------------------------------------

NAMES = ("nebu",)


def cue(device: str, action: str, utterance: str):
    classify = fn("orchestrator.utterance_kind", "classify_utterance")
    kind = classify(utterance, NAMES).kind
    return fn("orchestrator.access_target", "write_cue_problem")(
        device, action, utterance, kind_hint=kind, assistant_names=NAMES
    )


def cue_without_kind(device: str, action: str, utterance: str):
    """The call as it was made before the clause rule: the whole utterance, no classification."""
    return fn("orchestrator.access_target", "write_cue_problem")(device, action, utterance)


def verdict(utterance: str):
    uk = fn("orchestrator.utterance_kind", "classify_utterance")(utterance, NAMES)
    return uk.kind.name, uk.command_text


NOW = 1_800_000_000.0


def reply_form(query: str, assistant: str, intent: str = "dining"):
    followup = importlib.import_module("orchestrator.followup")
    last = {"user": "any good ramen places nearby?", "assistant": assistant, "at": NOW - 20, "session_class": "other"}
    task = followup.open_task(last, intent, None, session_class="other", now=NOW)
    if task is None:
        return "no_task"
    classify = fn("orchestrator.utterance_kind", "classify_utterance")
    result = followup.resolve(query, task, classify(query), None)
    return type(result).__name__ if result is not None else None


OFFER = "I found two spots. Want me to check which one has a patio?"


def continuation(query: str) -> bool:
    return fn("orchestrator.context.detector", "detect_context_reference")(query)["is_continuation"]


def identity_kind(query: str, names=()):
    return fn("orchestrator.identity_answer", "identity_question")(query, names)


def arbitrate(query, llm, llm_conf, pattern, pattern_conf=0.85):
    I = fn("orchestrator.state", "IntentCategory")
    out = fn("orchestrator.intent_arbitration", "arbitrate_intent")(
        query=query, llm_intent=getattr(I, llm), llm_confidence=llm_conf,
        pattern_intent=getattr(I, pattern), pattern_confidence=pattern_conf,
    )
    return out.intent.name, out.rule


def is_fragment(query: str, intent: str = "unknown", **extra) -> bool:
    state = NS(intent=intent, query=query, prev_context=None, conversation_history=None, context_ref_info=None, **extra)
    return fn("orchestrator.fast_path", "fragment_shaped_unknown")(state)


def cacheable(intent: str, **flags) -> bool:
    I = fn("orchestrator.state", "IntentCategory")
    base = dict(grounded=True, is_fallback=False, identity_answer=False, local_business_cue=False, ungrounded=False)
    return fn("orchestrator.semantic_cache", "cacheable_turn")(getattr(I, intent), **{**base, **flags})


def replay_entry_usable(intent: str, **metadata) -> bool:
    entry = {"intent": intent, "_cache_metadata": metadata}
    return fn("orchestrator.semantic_cache", "cached_entry_usable")(entry, "a question")


def spoken_failure(**kwargs):
    gw = importlib.import_module("gateway.main")
    request = gw.ChatCompletionRequest(model="m", messages=[gw.ChatMessage(role="user", content="hello there")])
    return gw, gw._failure_completion(request, **kwargs)


def failure_text() -> str:
    _gw, completion = spoken_failure()
    return completion.choices[0].message.content


def failure_status(kind: str):
    import httpx

    gw = importlib.import_module("gateway.main")
    request = httpx.Request("POST", "http://upstream.invalid/x")
    if kind == "transport":
        return gw._failure_status(httpx.ConnectError("refused"))
    return gw._failure_status(
        httpx.HTTPStatusError("bad", request=request, response=httpx.Response(int(kind), request=request))
    )


def game(state, home, away, home_name="Harbor Gulls", away_name="Ridge Hawks"):
    return {"home_team": home_name, "away_team": away_name, "home_score": home, "away_score": away,
            "state": state, "status": "x"}


def headline(*rows):
    from datetime import datetime, timezone

    out = fn("orchestrator.sports_status", "status_headline")([], [], list(rows), datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc))
    return out["state"], out["winner"], out.get("tie")


def door(name: str) -> bool:
    return fn("orchestrator.access_target", "is_access_lock")("lock.sample", {"friendly_name": name})


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rag_resolved(variant: str) -> str:
    """Which candidate get_rag_service_url returns when the override is dead."""
    helpers = importlib.import_module("orchestrator.helpers")
    getattr(helpers, "_reset_reachability_state", lambda: None)()

    async def scenario():
        async def handle(_r, w):
            w.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        live = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        dead = f"http://127.0.0.1:{_closed_port()}"
        candidates = {
            "dead_override_live_registry": (dead, live, dead),
            "dead_override_live_env": (dead, None, live),
            "live_override_wins": (live, dead, dead),
        }[variant]
        override, registry, env = candidates

        class Admin:
            async def get_intent_routing(self):
                return {"weather": {"rag_service_url": override}}

        async def lookup(_intent):
            return registry

        saved = (helpers.get_admin_client, helpers.registry_get_service_url, helpers.WEATHER_SERVICE_URL)
        helpers.get_admin_client = lambda: Admin()
        helpers.registry_get_service_url = lookup
        helpers.WEATHER_SERVICE_URL = env
        try:
            got = await helpers.get_rag_service_url("weather")
        finally:
            helpers.get_admin_client, helpers.registry_get_service_url, helpers.WEATHER_SERVICE_URL = saved
            server.close()
        return "override" if got == override else "registry" if got == registry else "env" if got == env else "other"

    try:
        return asyncio.run(scenario())
    finally:
        getattr(helpers, "_reset_reachability_state", lambda: None)()


def grounded_state(data, intent="dining"):
    return {"retrieved_data": data, "intent": intent}


INVENTED = "Try Marlow's Kitchen on Cedar Street. It's open until 9 PM and the dishes run about $20."
EXPERIENCE = "I've eaten there twice and the noodles were wonderful."
KNOWLEDGE_ANSWER = "A stock split divides each share into several shares, so a $100 share becomes two $50 shares."
ERROR = {"error": "upstream failed", "success": False}

# --- the table -------------------------------------------------------------------------------------------
# (family, row id, call, expected)

ROWS = [
    # T3: a command is read from its own clause; narration and statements never act.
    ("T3", "narration-neighbours", lambda: cue("light", "turn_off", "the neighbours turn their lights off at midnight"), "no_cue"),
    ("T3", "narration-lamp-kept-on", lambda: cue("light", "turn_on", "we always leave the lamp on in the hall"), "no_cue"),
    ("T3", "preference-purple", lambda: cue("light", "set_color", "my daughter likes the lights purple"), "no_cue"),
    ("T3", "reported-command-after", lambda: cue("light", "turn_off", "dim the lights, my son said"), "no_cue"),
    ("T3", "reported-told-me", lambda: cue("light", "turn_off", "she told me to turn off the lights"), "no_cue"),
    ("T3", "schedule-statement", lambda: cue("light", "turn_on", "the hall lamp turns on at dusk"), "no_cue"),
    ("T3", "thermostat-statement", lambda: cue("climate", "set_temperature", "she wants the thermostat at 70"), "no_cue"),
    ("T3", "command-porch", lambda: cue("light", "turn_off", "turn off the porch light"), None),
    ("T3", "command-polite-tail", lambda: cue("light", "turn_on", "hallway lights on please"), None),
    ("T3", "request-first-person", lambda: cue("light", "set_color", "I want the lights green"), None),
    ("T3", "command-lock", lambda: cue("lock", "lock", "please lock the front door"), None),
    ("T3", "clause-negated-then-command", lambda: verdict("don't dim the den lights, turn on the study lamp"),
     ("IMPERATIVE", "turn on the study lamp")),
    ("T3", "clause-bare-device-off", lambda: verdict("kitchen lamp off"), ("IMPERATIVE", "kitchen lamp off")),
    ("T3", "clause-narration-is-unknown", lambda: verdict("when the movie starts the lights dim"), ("UNKNOWN", None)),
    # The whole-utterance read: before the clause rule, "on" and "lights" anywhere backed the write.
    ("T3", "base-read-kids-keep-lights-on", lambda: cue_without_kind("light", "turn_off", "the kids keep the lights on all night"), "no_cue"),

    # T2: a reply keeps its subject; trailing punctuation doesn't break a continuation.
    ("T2", "continuation-period", lambda: continuation("Yes."), True),
    ("T2", "continuation-exclamation", lambda: continuation("yes!"), True),
    ("T2", "continuation-comma-phrase", lambda: continuation("Sure, go ahead."), True),
    ("T2", "continuation-no-thanks-period", lambda: continuation("no thanks."), True),
    ("T2", "continuation-command-is-not", lambda: continuation("okay, turn off the lights"), False),
    ("T2", "affirm-resumes-the-request", lambda: reply_form("Yes.", OFFER), "Affirm"),
    ("T2", "decline-ends-the-topic", lambda: reply_form("nah", OFFER), "Decline"),
    ("T2", "slot-fills-the-blank", lambda: reply_form("in the east end", OFFER), "Slot"),
    ("T2", "command-is-never-a-reply", lambda: reply_form("turn on the lights", OFFER), None),
    ("T2", "unrelated-question-is-no-reply", lambda: reply_form("what about tomorrow", OFFER), None),
    ("T2", "no-offer-no-task", lambda: reply_form("yes", "It is sunny and mild today."), "no_task"),
    ("T2", "control-intent-no-task", lambda: reply_form("yes", OFFER, intent="control"), "no_task"),

    # T7: an unknown speaker is never told the owner's name or address.
    ("T7", "name-contraction", lambda: identity_kind("whats my name"), "name"),
    ("T7", "name-who-am-i", lambda: identity_kind("who am I"), "name"),
    ("T7", "name-vocative", lambda: identity_kind("friday, do you know my name", ["Friday"]), "name"),
    ("T7", "home-where-live", lambda: identity_kind("where do I live"), "home"),
    ("T7", "home-address", lambda: identity_kind("tell me my address"), "home"),
    ("T7", "house-question-is-not-identity", lambda: identity_kind("who lives here"), None),
    ("T7", "other-person-is-not-identity", lambda: identity_kind("what is her name"), None),
    ("T7", "reply-only-for-nobody-addressed", lambda: fn("orchestrator.identity_answer", "reply_for")("say my name", None) is not None, True),
    ("T7", "no-reply-for-a-household-member",
     lambda: fn("orchestrator.identity_answer", "reply_for")("say my name", "household") == fn("orchestrator.identity_answer", "NAME_REPLY"), False),
    ("T7", "backstop-replaces-a-leaked-name",
     lambda: fn("orchestrator.identity_answer", "backstop")("what's my name", "Your name is Sample Person.", None, "Sample Person", "1 Test Way, Nowhere")
     == fn("orchestrator.identity_answer", "NAME_REPLY"), True),
    ("T7", "backstop-leaves-a-named-caller",
     lambda: fn("orchestrator.identity_answer", "backstop")("what's my name", "Your name is Sample Person.", "owner", "Sample Person"), None),

    # T5: speech carries no markdown.
    ("T5", "bold", lambda: fn("shared.tts_normalizer", "normalize_for_tts")("Here are **three** options"), "Here are three options"),
    ("T5", "numbered-bold", lambda: fn("shared.tts_normalizer", "normalize_for_tts")("1. **First** do this"), "First do this."),
    ("T5", "inline-code", lambda: fn("shared.tts_normalizer", "normalize_for_tts")("Use `code` here"), "Use code here"),
    ("T5", "link", lambda: fn("shared.tts_normalizer", "normalize_for_tts")("See [the guide](http://example.invalid/g) now"), "See the guide now"),
    ("T5", "heading-and-bullets", lambda: fn("shared.tts_normalizer", "strip_markdown")("# Plan\n- item one\n- item two"), "Plan\nitem one.\nitem two."),
    ("T5", "plain-text-unchanged", lambda: fn("shared.tts_normalizer", "strip_markdown")("Plain sentence stays."), "Plain sentence stays."),
    ("T5", "idempotent", lambda: fn("shared.tts_normalizer", "strip_markdown")(fn("shared.tts_normalizer", "strip_markdown")("**a** and *b*")), "a and b"),

    # T4: arbitration, entity repair, fragments.
    ("T4", "arbitrate-pattern-beats-general-info", lambda: arbitrate("how did the lions do last night", "GENERAL_INFO", 0.9, "SPORTS"),
     ("SPORTS", "pattern_over_general_info")),
    ("T4", "arbitrate-score-question", lambda: arbitrate("what is the score of the eagles game", "GENERAL_INFO", 0.8, "SPORTS"),
     ("SPORTS", "pattern_over_general_info")),
    ("T4", "arbitrate-unknown-takes-pattern", lambda: arbitrate("is the packers game on", "UNKNOWN", 0.1, "SPORTS"),
     ("SPORTS", "unknown_takes_pattern")),
    ("T4", "arbitrate-book-title-stays-general", lambda: arbitrate("tell me about the lord of the rings books", "GENERAL_INFO", 0.9, "SPORTS"),
     ("GENERAL_INFO", "llm_stands")),
    ("T4", "repair-score-and-team", lambda: fn("orchestrator.entity_repair", "repair_for_routing")("whats the scor of the eagles game").text,
     "whats the score of the eagles game"),
    ("T4", "repair-dropped-letter", lambda: fn("orchestrator.entity_repair", "repair_for_routing")("who won the pakers game").text,
     "who won the packers game"),
    ("T4", "repair-first-letter", lambda: fn("orchestrator.entity_repair", "repair_for_routing")("core of the lions game").text,
     "score of the lions game"),
    ("T4", "repair-leaves-ordinary-words", lambda: fn("orchestrator.entity_repair", "repair_for_routing")("is my flight delayed").text,
     "is my flight delayed"),
    ("T4", "fragment-filler", lambda: is_fragment("mm hm uh"), True),
    ("T4", "fragment-two-words", lambda: is_fragment("pasta tonight"), True),
    ("T4", "fragment-question-form-is-not", lambda: is_fragment("what is the weather"), False),
    ("T4", "fragment-question-mark-is-not", lambda: is_fragment("pasta tonight?"), False),
    ("T4", "fragment-reference-word-is-not", lambda: is_fragment("the green one"), False),
    ("T4", "fragment-known-intent-is-not", lambda: is_fragment("pasta tonight", intent="dining"), False),

    # T6: a failure is spoken; the cache stores only what a tool or a vetted answer stands behind.
    ("T6", "failure-phrase", failure_text, "Sorry, I couldn't finish that. Please try again in a moment."),
    ("T6", "failure-counts-as-an-error", lambda: spoken_failure()[0]._turn_status(spoken_failure()[1]), "error"),
    ("T6", "failure-keeps-the-session", lambda: spoken_failure(return_session_id=True, session_id="sess-1")[1][1], "sess-1"),
    ("T6", "failure-status-5xx", lambda: failure_status("503"), 503),
    ("T6", "failure-status-transport", lambda: failure_status("transport"), None),
    ("T6", "breaker-ignores-a-500", lambda: 500 in importlib.import_module("gateway.main").BREAKER_FAILURE_STATUSES, False),
    ("T6", "cache-grounded-dining", lambda: cacheable("DINING"), True),
    ("T6", "cache-ungrounded-dining", lambda: cacheable("DINING", grounded=False), False),
    ("T6", "cache-websearch-never", lambda: cacheable("WEBSEARCH"), False),
    ("T6", "cache-control-never", lambda: cacheable("CONTROL"), False),
    ("T6", "cache-identity-never", lambda: cacheable("GENERAL_INFO", identity_answer=True), False),
    ("T6", "cache-fallback-never", lambda: cacheable("WEATHER", is_fallback=True), False),
    ("T6", "cache-local-business-general-info", lambda: cacheable("GENERAL_INFO", local_business_cue=True), False),
    ("T6", "cache-replay-needs-the-grounded-mark", lambda: replay_entry_usable("dining"), False),
    ("T6", "cache-replay-with-the-grounded-mark", lambda: replay_entry_usable("dining", grounded=True), True),

    # T10: a live game is live; a door is a door; a dead URL is skipped.
    ("T10", "sports-live-has-no-winner", lambda: headline(game("in", "3", "1")), ("live", None, None)),
    ("T10", "sports-final-winner-from-score", lambda: headline(game("post", "2", "5")), ("final", "Ridge Hawks", False)),
    ("T10", "sports-final-tie", lambda: headline(game("post", "3", "3")), ("final", None, True)),
    ("T10", "sports-upcoming-has-no-winner", lambda: headline(game("pre", "0", "0"))[1], None),
    ("T10", "sports-league-code", lambda: fn("orchestrator.sports_status", "live_league_code")("hockey/nhl"), "nhl"),
    ("T10", "sports-unknown-league-is-none", lambda: fn("orchestrator.sports_status", "live_league_code")("not a league"), None),
    ("T10", "lock-front-door", lambda: door("Front Door"), True),
    ("T10", "lock-entry-door", lambda: door("Mudroom Entry"), True),
    ("T10", "lock-child-lock-is-no-door", lambda: door("Spare Room Child Lock"), False),
    ("T10", "lock-keypad-is-no-door", lambda: door("Workshop Keypad"), False),
    ("T10", "lock-safe-is-no-door", lambda: door("Cabin Safe"), False),
    ("T10", "rag-dead-override-live-registry", lambda: rag_resolved("dead_override_live_registry"), "registry"),
    ("T10", "rag-dead-override-live-env", lambda: rag_resolved("dead_override_live_env"), "env"),
    ("T10", "rag-live-override-still-wins", lambda: rag_resolved("live_override_wins"), "override"),

    # T1: a tool owns the answer; an answer with no data behind it claims nothing.
    ("T1", "grounded-tool-result", lambda: fn("orchestrator.grounding", "grounded")(grounded_state({"tool_results": {"search": {"places": [{"name": "x"}]}}})), True),
    ("T1", "grounded-all-tools-errored", lambda: fn("orchestrator.grounding", "grounded")(grounded_state({"tool_results": {"search": ERROR}})), False),
    ("T1", "grounded-call-list-only", lambda: fn("orchestrator.grounding", "grounded")(grounded_state({"tool_calls": [{"name": "search"}]})), False),
    ("T1", "grounded-no-data", lambda: fn("orchestrator.grounding", "grounded")(grounded_state(None)), False),
    ("T1", "grounded-retrieve-path", lambda: fn("orchestrator.grounding", "grounded")(grounded_state({"current": {"temp": 70}}, "weather")), True),
    ("T1", "ungrounded-invented-venue",
     lambda: fn("orchestrator.grounding", "ungrounded_answer")(INVENTED, fn("orchestrator.state", "IntentCategory").DINING, "suggest a place for dinner", False), True),
    ("T1", "ungrounded-same-answer-with-data",
     lambda: fn("orchestrator.grounding", "ungrounded_answer")(INVENTED, fn("orchestrator.state", "IntentCategory").DINING, "suggest a place for dinner", True), False),
    ("T1", "ungrounded-experience-claim",
     lambda: fn("orchestrator.grounding", "ungrounded_answer")(EXPERIENCE, fn("orchestrator.state", "IntentCategory").GENERAL_INFO, "is it good", False), True),
    ("T1", "ungrounded-knowledge-example-is-fine",
     lambda: fn("orchestrator.grounding", "ungrounded_answer")(KNOWLEDGE_ANSWER, fn("orchestrator.state", "IntentCategory").GENERAL_INFO, "what is a stock split", False), False),
    ("T1", "local-business-nearby", lambda: fn("orchestrator.grounding", "local_business_cue")("is there a good thai place nearby"), True),
    ("T1", "local-business-coffee-around-here", lambda: fn("orchestrator.grounding", "local_business_cue")("find me a coffee shop around here"), True),
    ("T1", "local-business-origin-question-is-not", lambda: fn("orchestrator.grounding", "local_business_cue")("where did sushi originate"), False),
    ("T1", "local-business-definition-is-not", lambda: fn("orchestrator.grounding", "local_business_cue")("what is happy hour"), False),
    ("T1", "knowledge-definition", lambda: fn("orchestrator.grounding", "knowledge_question")("what is a mortgage"), True),
    ("T1", "knowledge-how-it-works", lambda: fn("orchestrator.grounding", "knowledge_question")("explain how compound interest works"), True),
    ("T1", "knowledge-current-state-is-not", lambda: fn("orchestrator.grounding", "knowledge_question")("what is a dividend today"), False),
    ("T1", "knowledge-find-a-business-is-not", lambda: fn("orchestrator.grounding", "knowledge_question")("what is a good pizza place near me"), False),
    ("T1", "location-amenity-phrase-is-no-place", lambda: fn("orchestrator.grounding", "plausible_location")("outdoor seating", "a thai place with outdoor seating"), False),
    ("T1", "location-named-place-is-one", lambda: fn("orchestrator.grounding", "plausible_location")("Riverton", "a thai place in Riverton"), True),
]

FAMILIES = ("T3", "T2", "T7", "T5", "T4", "T6", "T10", "T1")
FLOOR = 7  # per family: several paraphrases of each kind of shape, not one
# One row per family whose value was different before the campaign, and what it was.
NAMED = {
    "T3": "base-read-kids-keep-lights-on",    # None: the whole sentence's "on" and "lights" backed the write
    "T2": "continuation-period",              # False: trailing punctuation broke the continuation match
    "T7": "name-who-am-i",                    # no identity classifier: the model answered with the owner's name
    "T5": "bold",                             # asterisks reached the speaker
    "T4": "arbitrate-pattern-beats-general-info",  # the classifier's GENERAL_INFO won over a team-score pattern
    "T6": "failure-phrase",                   # a bare 502 with no spoken text
    "T10": "rag-dead-override-live-registry",  # the dead override came back first
    "T1": "ungrounded-invented-venue",        # the invented venue went out as the answer
}


def _ids():
    return [f"{family}-{row_id}" for family, row_id, _call, _expected in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_ids())
def test_the_replayed_shape_gets_its_answer(row):
    family, row_id, call, expected = row
    assert call() == expected, f"{family}/{row_id}"


def test_row_ids_are_unique():
    ids = _ids()
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("family", FAMILIES)
def test_each_family_has_its_population_and_its_named_member(family):
    rows = [r for r in ROWS if r[0] == family]
    assert len(rows) >= FLOOR, f"{family} has {len(rows)} rows"
    assert NAMED[family] in {r[1] for r in rows}


def test_every_family_in_the_table_is_a_declared_one():
    assert {r[0] for r in ROWS} == set(FAMILIES) == set(NAMED)


def test_the_boolean_rows_are_balanced_so_a_constant_function_cannot_pass():
    """A family whose rows all expect the same boolean would pass a function that returns it."""
    for family in FAMILIES:
        booleans = {r[3] for r in ROWS if r[0] == family and isinstance(r[3], bool)}
        assert booleans in (set(), {True, False}), family


def test_no_row_carries_a_real_looking_address_or_phone_number():
    import inspect
    import re

    source = inspect.getsource(importlib.import_module(__name__))
    assert not re.search(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b", source)
    assert not re.search(r"\b\d{2,5} [A-Z][a-z]+ (?:Street|Avenue|Road|Lane|Drive)\b", source)

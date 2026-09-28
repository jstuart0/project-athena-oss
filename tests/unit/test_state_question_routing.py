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
]

REFERENT_QUESTIONS = [
    "did those come back on?",
    "is it on",
    "are they off",
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

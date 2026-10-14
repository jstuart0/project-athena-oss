"""LLM against pattern: the rules `arbitrate_intent` applies."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "src")

from orchestrator.intent_arbitration import arbitrate_intent
from orchestrator.state import IntentCategory as I

TEAM_SCORE = "whats the score of the ravens game"
G, S, C, U = I.GENERAL_INFO, I.SPORTS, I.CONTROL, I.UNKNOWN


def _arb(query, llm, llm_conf, pat, pat_conf=0.85):
    return arbitrate_intent(query=query, llm_intent=llm, llm_confidence=llm_conf,
                            pattern_intent=pat, pattern_confidence=pat_conf)


@pytest.mark.parametrize("llm_conf", [0.5, 0.72, 0.75, 0.76, 0.9, 0.99])
def test_a_specific_pattern_beats_general_info_at_any_llm_confidence(llm_conf):
    out = _arb(TEAM_SCORE, G, llm_conf, S)
    assert (out.intent, out.confidence) == (S, 0.85)


@pytest.mark.parametrize("llm_conf", [0.5, 0.72, 0.9, 0.99])
def test_game_of_thrones_stays_general(llm_conf):
    assert _arb("tell me about the game of thrones books", G, llm_conf, S).intent == G
    assert _arb("game of thrones cast", G, llm_conf, S).intent == G


def test_unknown_takes_a_specific_pattern_and_keeps_a_fallback_unknown():
    assert _arb(TEAM_SCORE, U, 0.1, S).intent == S
    assert _arb("hmm interesting point", U, 0.1, G, 0.5).intent == U


def test_the_boost_applies_only_when_they_agree():
    agree = _arb(TEAM_SCORE, S, 0.6, S, 0.85)
    assert (agree.intent, agree.confidence, agree.rule) == (S, 0.85, "agreement_boost")
    disagree = _arb("what is the weather like", I.NEWS, 0.6, I.WEATHER, 0.85)
    assert disagree.intent == I.WEATHER and disagree.rule != "agreement_boost"


@pytest.mark.parametrize("llm_conf,expected", [(0.65, I.WEATHER), (0.72, I.WEATHER), (0.75, I.WEATHER), (0.8, I.NEWS)])
def test_a_disagreeing_unsure_llm_gives_way_with_no_dead_zone_below_the_ceiling(llm_conf, expected):
    assert _arb("what is the weather like", I.NEWS, llm_conf, I.WEATHER).intent == expected


def test_the_kept_overrides_still_apply():
    assert _arb("take it easy on my eyes", G, 0.95, C).intent == C                    # implicit brightness
    assert _arb("it's mad cold in here", G, 0.95, C).intent == C                       # temperature complaint
    assert _arb("gimme some blue lights", G, 0.95, C).intent == C                      # colour request
    assert _arb("whats the temperature inside", I.WEATHER, 0.95, C).intent == C        # indoor temperature
    assert _arb("tv won't turn on", C, 0.95, G).intent == G                            # problem, not a command
    assert _arb("search the web for pizza", G, 0.95, I.WEBSEARCH).intent == I.WEBSEARCH
    assert _arb("tell me about turning on lights", G, 0.95, C).intent == G             # no cue phrase: general stands


def test_indoor_temperature_question_sets_the_climate_entities():
    out = _arb("is it cold in here", I.WEATHER, 0.9, I.WEATHER)
    assert out.intent == C and out.entities_update == {"device_type": "climate", "action": "get_status"}


def test_it_logs_the_rule_and_no_query_text(capsys):
    _arb(TEAM_SCORE, G, 0.9, S)
    logged = capsys.readouterr().out
    assert "intent_arbitration" in logged and "pattern_over_general_info" in logged
    assert "ravens" not in logged

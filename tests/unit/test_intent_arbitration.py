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


W = I.WEATHER


@pytest.mark.parametrize("query", [
    "how cold is it outside",
    "will it rain tomorrow",
    "is it going to snow",
    "chance of rain this weekend",
    "any sleet or hail coming",
    "is there a storm tonight",
    "what's the outside temperature",
    "show me the forecast",
    "show me the weather",
])
@pytest.mark.parametrize("llm_conf", [0.5, 0.9, 0.99])
def test_weather_phrasing_takes_the_pattern_over_general_info(query, llm_conf):
    assert _arb(query, G, llm_conf, W).intent == W


@pytest.mark.parametrize("query", [
    "play the rain album",
    "tell me about the movie storm",
    "who sings the song rain",
    "what is a train",
])
@pytest.mark.parametrize("llm_conf", [0.5, 0.9, 0.99])
def test_weather_words_in_media_or_other_words_stay_general(query, llm_conf):
    assert _arb(query, G, llm_conf, W).intent == G


# Definitional, historical and how/why wording stays general knowledge (reconcile r1, H3).
@pytest.mark.parametrize("query,pattern", [
    ("who invented basketball", I.SPORTS),
    ("what are the rules of football", I.SPORTS),
    ("explain the offside rule in soccer", I.SPORTS),
    ("what is the history of the nfl", I.SPORTS),
    ("history of the nfl", I.SPORTS),
    ("how does rain form", W),
    ("why does it snow in winter", W),
])
@pytest.mark.parametrize("llm_conf", [0.5, 0.9, 0.99])
def test_timeless_wording_stays_general_info(query, pattern, llm_conf):
    assert _arb(query, G, llm_conf, pattern).intent == G


@pytest.mark.parametrize("query,pattern", [
    ("what are the rules of football tonight", I.SPORTS),
    ("how did the falcons do today", I.SPORTS),
    ("whats the score of the giants game", I.SPORTS),
    ("how does the forecast look", W),
    ("why does it rain so much this week", W),
    ("is it going to snow", W),
])
def test_time_sensitive_wording_still_takes_the_pattern(query, pattern):
    assert _arb(query, G, 0.9, pattern).intent == pattern


@pytest.mark.parametrize("query,pattern", [
    ("who is the best basketball player", I.SPORTS),
    ("who is the greatest quarterback in the nfl", I.SPORTS),
    ("what is the weather like in paris in spring", W),
    ("what is the weather typically like in london in winter", W),
])
@pytest.mark.parametrize("llm_conf", [0.5, 0.9, 0.99])
def test_opinion_and_season_wording_stays_general_info(query, pattern, llm_conf):
    assert _arb(query, G, llm_conf, pattern).intent == G


@pytest.mark.parametrize("query,pattern", [
    ("who is the best team playing tonight in the nfl", I.SPORTS),
    ("what is the weather like today", W),
    ("what is the weather like in paris this weekend", W),
])
def test_opinion_wording_with_a_time_cue_still_takes_the_pattern(query, pattern):
    assert _arb(query, G, 0.9, pattern).intent == pattern


# --- "we" questions about the household's own team -------------------------------------------

CONTEST_QUESTIONS = [
    "how did we do last night",
    "how did we do in the game last night",
    "how'd we do yesterday",
    "did we win last night",
    "did we win the game",
    "did we beat them on sunday",
    "did we lose today",
    "who did we play last night",
    "did we beat them tonight",
    "how did we do in the game",
    "did we win last night",
    "what was our score last night",
    "what's our record this season",
]
NOT_CONTEST = [
    "how did we do on the project",
    "did we win the lottery",
    "did we do the dishes today",
    "how do we play chess",
    "did we win",
    "how are we doing today",
    "did we win the raffle today",
    "did we beat traffic today",
    "how did we do on the test today",
    "can we play a game",
    "can we play a game tonight",
    "how do we play this game",
    "how do we play this game tonight",
    "are we playing board games tonight",
    "who invented the game we played last night",
    "how did we do on the project yesterday",
    "did we win the lottery last night",
    "we won the lottery last night",
    "how did you do last night",
    "what did we have for dinner last night",
]


@pytest.mark.parametrize("query", CONTEST_QUESTIONS)
def test_a_we_contest_question_with_a_game_or_time_cue_is_sports(query):
    from orchestrator.intent_arbitration import is_team_contest_question
    assert is_team_contest_question(query)
    out = _arb(query, G, 0.95, G, 0.5)  # the observed LLM answer: general_info 0.95; the pattern fallback says 0.5
    assert (out.intent, out.rule) == (S, "team_contest_question")


@pytest.mark.parametrize("query", NOT_CONTEST)
def test_we_questions_without_a_contest_and_a_cue_are_not_sports(query):
    from orchestrator.intent_arbitration import is_team_contest_question
    assert not is_team_contest_question(query)
    assert _arb(query, G, 0.95, G, 0.5).intent == G


def test_the_rule_is_not_held_back_by_the_timeless_wording_list():
    from orchestrator.intent_arbitration import _TIMELESS_WORDING
    assert _TIMELESS_WORDING.search("how did we do last night")
    assert _arb("how did we do last night", G, 0.99, G, 0.5).intent == S


def test_an_explicit_web_search_still_wins_over_the_contest_rule():
    assert _arb("search the web: did we win last night", G, 0.9, I.WEBSEARCH, 0.9).intent == I.WEBSEARCH

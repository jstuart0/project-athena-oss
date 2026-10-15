"""The grounding contract (orchestrator.grounding) and the cache rules built on it.

Pure functions over synthetic states and utterances: which intents a tool owns,
when a turn holds tool data, the forced call a data turn makes, the location a
search is near, when an answer claims what only data could give, and which
finished turns the semantic cache may store.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")

from orchestrator import grounding, semantic_cache  # noqa: E402
from orchestrator.model_safe_errors import tool_error_result  # noqa: E402
from orchestrator.state import IntentCategory, OrchestratorState  # noqa: E402

I = IntentCategory

ALL_TOOLS = {
    "search_restaurants", "get_sports_scores", "get_weather", "get_news", "search_events",
    "search_recipes", "search_streaming", "get_airport_info", "search_flights", "scrape_website",
}


def _state(query="recommend an italian place near me", intent=I.DINING, **fields):
    return OrchestratorState(query=query, intent=intent, **fields)


def _tool_data(*results):
    return {
        "tool_calls": ["t"] * len(results),
        "tool_results": {f"id{n}": r for n, r in enumerate(results)},
    }


# --- grounded -----------------------------------------------------------------

def test_a_successful_tool_result_grounds_the_turn():
    assert grounding.grounded(_state(retrieved_data=_tool_data({"places": [{"name": "x"}]})))


def test_grounded_is_false_when_the_only_tool_result_is_an_error_object():
    state = _state(retrieved_data=_tool_data(tool_error_result("boom")))
    assert not grounding.grounded(state)


def test_one_success_among_failures_grounds_the_turn():
    state = _state(retrieved_data=_tool_data(tool_error_result("boom"), {"places": [1]}))
    assert grounding.grounded(state)


@pytest.mark.parametrize("data", [
    {},
    {"tool_calls": ["search_restaurants"]},
    _tool_data(None),
    _tool_data({}),
    _tool_data({"success": False, "message": "no"}),
    {"type": "conversation_context", "previous_response": "x"},
    {"limitation": True, "message": "too far"},
])
def test_these_are_not_data(data):
    assert not grounding.grounded(_state(retrieved_data=data))


def test_a_non_empty_retrieve_path_result_grounds_the_turn():
    assert grounding.grounded(_state(intent=I.WEATHER, retrieved_data={"current": {"temp": 70}}))


def test_grounded_reads_a_dict_state_like_the_graph_output():
    assert grounding.grounded({"retrieved_data": {"current": {"temp": 70}}})
    assert not grounding.grounded({"retrieved_data": None})


# --- forced calls ---------------------------------------------------------------

@pytest.mark.parametrize("intent,query,tool,key", [
    (I.DINING, "recommend an italian place near me", "search_restaurants", "term"),
    (I.WEATHER, "what is the weather tomorrow", "get_weather", "location"),
    (I.NEWS, "any news about the election", "get_news", "query"),
    (I.EVENTS, "concerts this weekend", "search_events", "keyword"),
    (I.RECIPES, "a recipe for lentil soup", "search_recipes", "query"),
    (I.STREAMING, "where can I watch the show", "search_streaming", "query"),
    (I.AIRPORTS, "terminal info for the airport", "get_airport_info", "query"),
    (I.FLIGHTS, "flights to the coast", "search_flights", "query"),
])
def test_a_data_intent_forces_its_tool(intent, query, tool, key):
    calls = grounding.forced_tool_calls(
        _state(query, intent), offered=ALL_TOOLS, search_location="Anytown",
    )
    assert [c["function"]["name"] for c in calls] == [tool]
    assert key in calls[0]["function"]["arguments"]


def test_sports_forces_the_team_from_the_entities():
    state = _state("how did the home team do", I.SPORTS, entities={"team": "Anytown Owls"})
    calls = grounding.forced_tool_calls(state, offered=ALL_TOOLS, search_location="x")
    assert calls[0]["function"] == {"name": "get_sports_scores", "arguments": {"team": "Anytown Owls"}}


def test_sports_without_a_team_is_not_forced(monkeypatch):
    monkeypatch.setattr(grounding, "_sports_team", lambda state: None)
    assert grounding.forced_tool_calls(_state("any game on", I.SPORTS), offered=ALL_TOOLS, search_location="x") == []


def _forced_sports(default_team, **fields):
    state = _state("how did we do last night", I.SPORTS, **fields)
    calls = grounding.forced_tool_calls(state, offered=ALL_TOOLS, search_location="x", default_team=default_team)
    return calls[0]["function"]["arguments"] if calls else None


def test_a_favourite_with_a_league_in_the_tool_enum_is_forced_with_its_league():
    from shared.favorite_teams import FavoriteTeam

    assert _forced_sports(FavoriteTeam(name="Owls", league="nfl")) == {"team": "Owls", "league": "nfl"}


@pytest.mark.parametrize("league", ["ligue-1", "liga-mx", "europa-league", None])
def test_a_favourite_outside_the_tool_enum_or_with_no_league_is_forced_by_name_only(league):
    from shared.favorite_teams import FavoriteTeam

    assert _forced_sports(FavoriteTeam(name="Owls", league=league)) == {"team": "Owls"}


def test_the_tool_enum_here_matches_the_tool_schema():
    from orchestrator import rag_tools

    schema = next(
        t["function_schema"]["function"]
        for t in rag_tools.TOOL_DEFINITIONS
        if t["function_schema"]["function"]["name"] == "get_sports_scores"
    )
    assert set(schema["parameters"]["properties"]["league"]["enum"]) == grounding._SPORTS_TOOL_LEAGUES


def test_no_favourite_and_no_entity_is_not_forced():
    assert _forced_sports(None) is None


def test_a_named_team_beats_the_favourite_in_the_forced_call():
    from shared.favorite_teams import FavoriteTeam

    args = _forced_sports(FavoriteTeam(name="Owls", league="nfl"), entities={"team": "Giants"})
    assert args == {"team": "Giants"}


def test_the_forced_team_never_comes_from_the_environment():
    import inspect

    assert "get_config" not in inspect.getsource(grounding._sports_team)
    assert "get_config" not in inspect.getsource(grounding._sports_arguments)
    assert "sports_default_team" not in inspect.getsource(grounding)


@pytest.mark.parametrize("intent", [I.STOCKS, I.DIRECTIONS, I.WEBSEARCH, I.MUSIC_PLAY, I.CONTROL, I.TESLA])
def test_these_intents_are_never_forced_here(intent):
    assert grounding.forced_tool_calls(_state("anything at all", intent), offered=ALL_TOOLS, search_location="x") == []


def test_nothing_is_forced_when_the_tool_is_not_offered():
    # The public audience is offered weather, news, recipes and streaming only;
    # an SMS from outside the stay only travel tools.
    public = {"get_weather", "get_news", "search_recipes", "search_streaming"}
    assert grounding.forced_tool_calls(_state(), offered=public, search_location="Anytown") == []
    assert grounding.forced_tool_calls(_state(), offered=set(), search_location="Anytown") == []
    assert grounding.forced_tool_calls(
        _state("what is the weather", I.WEATHER), offered=public, search_location="Anytown"
    )


def test_a_local_business_request_forces_the_place_search_even_as_general_info():
    for intent in (I.GENERAL_INFO, I.UNKNOWN):
        calls = grounding.forced_tool_calls(
            _state("find me a good pizza place nearby", intent), offered=ALL_TOOLS, search_location="Anytown"
        )
        assert [c["function"]["name"] for c in calls] == ["search_restaurants"]


def test_a_knowledge_question_as_general_info_is_not_forced():
    assert grounding.forced_tool_calls(
        _state("what is a stock split", I.GENERAL_INFO), offered=ALL_TOOLS, search_location="Anytown"
    ) == []


def test_a_follow_up_on_this_intents_tool_results_is_not_forced():
    previous = {"intent": "dining", "parameters": {"tool_calls": ["search_restaurants"], "grounded": True}, "response": "x"}
    state = _state("do they have outdoor seating", prev_context=previous)
    assert grounding.prior_tool_context(state)
    assert grounding.forced_tool_calls(state, offered=ALL_TOOLS, search_location="Anytown") == []


def test_a_retrieve_path_turn_with_data_counts_as_prior_context():
    previous = {"intent": "weather", "parameters": {"grounded": True, "last_data": {"current": {}}}, "response": "Sunny."}
    state = _state("what about tomorrow", I.WEATHER, prev_context=previous)
    assert grounding.prior_tool_context(state)
    assert grounding.tool_owned_without_data(_state("what about tomorrow", I.WEATHER, prev_context=None))
    assert not grounding.tool_owned_without_data(state)


@pytest.mark.parametrize("previous", [
    None,
    {"intent": "weather", "parameters": {"tool_calls": ["get_weather"], "grounded": True}},
    {"intent": "dining", "parameters": {"direct_answer": True}},
    {"intent": "dining", "parameters": {"tool_calls": []}},
])
def test_other_previous_contexts_do_not_stop_the_forced_call(previous):
    state = _state(prev_context=previous)
    assert not grounding.prior_tool_context(state)
    assert grounding.forced_tool_calls(state, offered=ALL_TOOLS, search_location="Anytown")


def test_a_forced_dining_call_carries_the_search_location_and_a_clean_term():
    call = grounding.forced_tool_calls(
        _state("Can you recommend a good Italian place near me please?"),
        offered=ALL_TOOLS, search_location="12 Juniper Lane",
    )[0]
    assert call["function"]["arguments"] == {"location": "12 Juniper Lane", "term": "italian place"}
    assert call["id"] and call["type"] == "function"


def test_no_search_location_means_no_forced_place_search():
    assert grounding.forced_tool_calls(_state(), offered=ALL_TOOLS, search_location="") == []


def test_forced_calls_read_the_resolved_request_when_a_turn_has_one():
    assert grounding.request_text(_state("yes", I.DINING)) == "yes"
    namespace = SimpleNamespace(query="yes", resolved_request="thai food nearby")
    assert grounding.request_text(namespace) == "thai food nearby"


# --- locations ------------------------------------------------------------------

@pytest.mark.parametrize("text,query,expected", [
    ("outdoor seating", "an italian place with outdoor seating", False),
    ("nearby", "something nearby", False),
    ("near me", "pizza near me", False),
    ("around here", "pizza around here", False),
    ("patio", "a patio please", False),
    ("rooftop", "a rooftop bar", False),
    ("happy hour", "bars with happy hour", False),
    ("downtown", "dinner downtown", False),
    ("Springfield", "dinner in Springfield", True),
    ("downtown Springfield", "dinner in downtown Springfield", True),
    ("Springfield", "dinner somewhere", False),  # not in the user's words
    ("", "dinner", False),
    (None, "dinner", False),
])
def test_plausible_location(text, query, expected):
    assert grounding.plausible_location(text, query) is expected


def test_the_device_position_beats_a_classifier_location():
    state = _state(
        "an italian place with outdoor seating",
        entities={"location": "outdoor seating"},
        context={"location_override": {"use_device_location": True, "latitude": 40.0, "longitude": -75.0}},
    )
    assert grounding.resolve_search_location(state, "home") == ("40.0000, -75.0000", "location_override")


def test_an_amenity_phrase_the_classifier_read_as_a_place_falls_back_to_home():
    state = _state("an italian place with outdoor seating", entities={"location": "outdoor seating"})
    assert grounding.resolve_search_location(state, "home") == ("home", "default")


def test_a_place_the_user_named_is_used():
    state = _state("dinner in Springfield", entities={"location": "Springfield"})
    assert grounding.resolve_search_location(state, "home") == ("Springfield", "classifier_entity")


def test_the_requests_own_location_is_used_when_it_is_not_in_the_words():
    state = _state("an italian place", entities={"location": "47.6, -122.3"}, request_location="47.6, -122.3")
    assert grounding.resolve_search_location(state, "home") == ("47.6, -122.3", "request_entities")


def test_an_entity_location_that_is_neither_in_the_words_nor_the_requests_own_is_dropped():
    state = _state("an italian place", entities={"location": "Atlantis"}, request_location="47.6, -122.3")
    assert grounding.resolve_search_location(state, "home") == ("home", "default")
    assert grounding.resolve_search_location(_state("an italian place", entities={"location": "Atlantis"}), "home") == ("home", "default")


# --- ungrounded answers -----------------------------------------------------------

INVENTED = "Try Luigi's Trattoria on Main Street. It's open until 10 PM and the pasta is around $18."


def test_an_invented_venue_with_hours_and_no_data_is_ungrounded():
    assert grounding.ungrounded_answer(INVENTED, I.DINING, "recommend an italian place", False)


def test_the_same_answer_with_tool_data_is_fine():
    assert not grounding.ungrounded_answer(INVENTED, I.DINING, "recommend an italian place", True)


@pytest.mark.parametrize("answer,query,intent", [
    ("A stock split divides each share into several shares and lowers the price per share.", "what is a stock split", I.GENERAL_INFO),
    ("Paris is the capital of France, and it is open to visitors all year.", "capital of France", I.GENERAL_INFO),
    ("Photosynthesis happens in the chloroplasts of a plant cell.", "how does photosynthesis work", I.GENERAL_INFO),
    ("I couldn't find that.", "recommend an italian place", I.DINING),
    ("Sure. Italian food is often a good choice for dinner.", "recommend an italian place", I.DINING),
])
def test_knowledge_and_plain_answers_are_never_ungrounded(answer, query, intent):
    assert not grounding.ungrounded_answer(answer, intent, query, False)


KNOWLEDGE_WITH_EXAMPLES = [
    ("what is a stock split", I.STOCKS,
     "A stock split divides each share into more shares. If Acme Corp trades at $50 and splits 2 for 1, "
     "you hold twice the shares at $25 each."),
    ("what is a stock split", I.GENERAL_INFO,
     "For example, Acme Corp at $50 becomes two shares of $25 after a 2 for 1 split."),
    ("how does a dividend work", I.STOCKS, "Say Globex Holdings pays $2 a year; you receive $2 per share."),
    ("explain what a game's point spread means", I.SPORTS,
     "If Anytown Owls are favored by 3, they must win by more than 3 points. A $10 bet pays $19.10."),
    ("why is the forecast sometimes wrong", I.WEATHER, "Forecast Models Inc. said 72 degrees at 3:30 PM but it was 65."),
    ("how do airlines price tickets", I.FLIGHTS, "Sky Lines might charge $300 for a 9:15 AM seat and $150 at night."),
    ("what is a prix fixe menu", I.DINING,
     "At Chez Example Bistro a prix fixe dinner is $45 and served until 10 PM."),
    ("what is a recipe for roux", I.RECIPES, "Cook equal parts butter and flour; Le Cordon Bleu says 5 minutes at $0 extra."),
]


@pytest.mark.parametrize("query,intent,answer", KNOWLEDGE_WITH_EXAMPLES,
                         ids=[f"{q[:28]}|{i.value}" for q, i, _a in KNOWLEDGE_WITH_EXAMPLES])
def test_a_knowledge_answer_with_a_worked_example_is_never_ungrounded(query, intent, answer):
    assert not grounding.ungrounded_answer(answer, intent, query, False)


def test_the_same_example_is_judged_when_the_request_is_for_a_business():
    answer = "Try Luigi's Trattoria. It's open until 10 PM and pasta is $18."
    assert grounding.ungrounded_answer(answer, I.STOCKS, "recommend an italian place near me", False)
    assert grounding.ungrounded_answer(answer, I.DINING, "dinner ideas", False)


@pytest.mark.parametrize("intent", [I.STOCKS, I.NEWS, I.SPORTS, I.WEATHER, I.RECIPES, I.STREAMING, I.AIRPORTS])
def test_only_venue_intents_are_judged_for_an_invented_venue(intent):
    answer = "Try Luigi's Trattoria. It's open until 10 PM and pasta is $18."
    assert not grounding.ungrounded_answer(answer, intent, "tell me something", False)
    assert grounding.ungrounded_answer(answer, I.DINING, "tell me something", False)
    assert grounding.ungrounded_answer(answer, I.EVENTS, "tell me something", False)


@pytest.mark.parametrize("query,expected", [
    ("what is a stock split", True), ("what's an index fund", True), ("how does a dividend work", True),
    ("why is the sky blue", True), ("explain compound interest", True), ("define inflation", True),
    ("what does a price to earnings ratio mean", True), ("the difference between a stock and a bond", True),
    ("what is the weather", False), ("what's the score of the game", False), ("find an italian place", False),
    ("what is a good restaurant near me", False), ("", False),
])
def test_knowledge_question(query, expected):
    assert grounding.knowledge_question(query) is expected


@pytest.mark.parametrize("answer,expected", [
    ("I've had a great meal there last week.", True),
    ("I went there last week and loved it.", True),
    ("When I visited, the pasta was great.", True),
    ("I've been to that place twice.", True),
    ("I ate there with friends.", True),
    ("My favorite cuisine is Thai.", False),
    ("I'd try the pasta.", False),
    ("My favorite spot for ideas is a library.", False),
    ("I recommend the tasting menu.", False),
    ("They say the pasta there is great.", False),
])
def test_experience_claim_targets_fabricated_visits_not_persona_preferences(answer, expected):
    assert grounding.experience_claim(answer, "where should we eat") is expected


def test_a_first_person_experience_is_ungrounded_for_any_intent():
    answer = "I've been there and the pasta is wonderful."
    assert grounding.ungrounded_answer(answer, I.GENERAL_INFO, "is it good", False)


def test_a_story_in_the_first_person_is_not_an_experience_claim():
    story = "I went to the market and I ate there, then I visited my grandmother."
    assert not grounding.ungrounded_answer(story, I.GENERAL_INFO, "tell me a story about a market", False)
    assert grounding.experience_claim(story, "is the market good")


def test_a_local_business_request_judged_even_when_the_intent_is_general_info():
    assert grounding.ungrounded_answer(INVENTED, I.GENERAL_INFO, "recommend an italian place near me", False)


# --- evidence summary ----------------------------------------------------------

def test_the_evidence_summary_is_bounded_and_skips_failures():
    big = {"places": [{"name": "x" * 500} for _ in range(40)]}
    state = _state(retrieved_data={
        "tool_calls": ["search_restaurants", "get_weather"],
        "tool_results": {"a": big, "b": tool_error_result("boom"), "c": {"current": {"temp": 70}}},
    })
    summary = grounding.evidence_summary(state)
    assert len(summary) <= 4000
    assert summary.startswith("Tools used: search_restaurants, get_weather")
    assert "boom" not in summary and "temp" in summary
    assert len(grounding.evidence_summary(state, limit=300)) <= 300


def test_the_evidence_summary_of_a_retrieve_result_is_truncated():
    state = _state(intent=I.WEATHER, retrieved_data={"current": "y" * 9000})
    assert len(grounding.evidence_summary(state)) == 4000
    assert grounding.evidence_summary(_state()) == ""


# --- local-business cue ------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("recommend an italian place near me", True),
    ("is there a bar with happy hour tomorrow", True),
    ("where can I get a good coffee shop", True),
    ("find a pizza place nearby", True),
    ("best brunch spots", True),
    ("any good sushi in town", True),
    ("book a table at a steakhouse tonight", True),
    ("what is a stock split", False),
    ("what's the capital of France", False),
    ("explain how a bar chart works", False),
    ("tell me a joke", False),
    ("", False),
    # knowledge questions that mention a venue word
    ("where did sushi originate", False),
    ("a good brunch recipe", False),
    ("what is happy hour", False),
    ("what's the history of the diner", False),
    ("how do i make a good pizza at home", False),
    ("why is brunch so popular", False),
    ("I like sushi", False),
    ("sushi is my favorite food", False),
    ("the pizza was cold", False),
])
def test_local_business_cue(query, expected):
    assert grounding.local_business_cue(query) is expected


# --- the cache allow-list ---------------------------------------------------------------

def test_every_intent_has_a_cache_category_and_nothing_else_does():
    assert set(semantic_cache.INTENT_CACHE_CATEGORY) == {i.value for i in IntentCategory}


def test_every_mapped_category_exists_in_the_ttl_table():
    for intent, category in semantic_cache.INTENT_CACHE_CATEGORY.items():
        assert category is None or category in semantic_cache.CACHE_TTL_CONFIG, intent


def test_music_play_is_never_cacheable_and_dining_is_a_named_member():
    assert semantic_cache.INTENT_CACHE_CATEGORY["music_play"] is None
    assert semantic_cache.INTENT_CACHE_CATEGORY["dining"] == "dining"
    assert semantic_cache.INTENT_CACHE_CATEGORY["websearch"] is None


def _turn(intent, **over):
    flags = dict(grounded=True, is_fallback=False, identity_answer=False, local_business_cue=False, ungrounded=False)
    flags.update(over)
    return semantic_cache.cacheable_turn(intent, **flags)


def test_a_new_intent_fails_closed():
    class Fake:
        value = "hologram_control"

    assert not _turn(Fake())
    assert not _turn("hologram_control")
    assert not _turn(None)
    assert _turn(I.WEATHER)  # the positive control: the helper can say yes


@pytest.mark.parametrize("intent", [
    I.CONTROL, I.MUSIC_PLAY, I.MUSIC_CONTROL, I.TV_CONTROL, I.TEXT_ME_THAT, I.NOTIFICATION_PREF,
    I.WEBSEARCH, I.TESLA, I.UNKNOWN,
])
def test_these_intents_are_never_cached(intent):
    assert not _turn(intent)


@pytest.mark.parametrize("intent", sorted(grounding.TOOL_OWNED, key=lambda i: i.value))
def test_a_tool_owned_intent_needs_tool_data(intent):
    assert _turn(intent, grounded=True)
    assert not _turn(intent, grounded=False)


@pytest.mark.parametrize("flag", ["is_fallback", "identity_answer"])
def test_a_fallback_or_identity_turn_is_never_cached(flag):
    assert not _turn(I.WEATHER, **{flag: True})
    assert not _turn(I.GENERAL_INFO, grounded=False, **{flag: True})


def test_general_info_is_cached_only_without_invented_specifics_or_a_local_business_cue():
    assert _turn(I.GENERAL_INFO, grounded=False)
    assert not _turn(I.GENERAL_INFO, grounded=False, ungrounded=True)
    assert not _turn(I.GENERAL_INFO, grounded=False, local_business_cue=True)


def test_a_failed_dining_search_is_not_cached():
    assert not _turn(I.DINING, grounded=False)


def test_a_stored_entry_is_replayed_only_under_the_same_rule():
    usable = semantic_cache.cached_entry_usable
    meta = {"grounded": True}
    assert usable({"intent": "weather", "_cache_metadata": meta}, "weather today")
    assert not usable({"intent": "weather", "_cache_metadata": {}}, "weather today")  # written before the flag
    assert not usable({"intent": "weather", "_cache_metadata": {"grounded": False}}, "weather today")
    assert not usable({"intent": "websearch", "_cache_metadata": meta}, "search the web")
    assert not usable({"intent": "hologram_control", "_cache_metadata": meta}, "x")
    assert not usable({"_cache_metadata": meta}, "x")
    assert usable({"intent": "general_info", "_cache_metadata": {}}, "what is a stock split")
    assert not usable({"intent": "general_info", "_cache_metadata": {}}, "recommend an italian place near me")
    assert usable({"intent": "general_info", "_cache_metadata": {"grounded": False}}, "what is a stock split")


def test_an_answer_stored_before_a_routing_rule_is_not_replayed_for_a_tool_owned_route():
    usable = semantic_cache.cached_entry_usable
    stale = {"intent": "general_info", "_cache_metadata": {"grounded": False}}
    assert usable(stale, "how did we do last night")                             # no rule routes it: replayable
    assert not usable(stale, "how did we do last night", routed_intent="sports")
    assert not usable({"intent": "unknown", "_cache_metadata": {}}, "q", routed_intent="weather")
    assert usable(stale, "what is a stock split", routed_intent="general_info")
    assert usable(stale, "what is a stock split", routed_intent=None)


def test_the_release_epoch_was_bumped_for_the_rule_change():
    assert semantic_cache.CACHE_EPOCH != "2026-10-r1"
    assert semantic_cache.CACHE_EPOCH in semantic_cache.FLUSH_MARKER_KEY


@pytest.mark.parametrize("query,expected", [
    ("Yes.", True), ("yes", True), ("YES!", True), ("no thanks.", True), ("nah", True), ("sure", True),
    ("sounds good", True), ("yes please", True),
    ("what is the weather", False), ("yes turn on the kitchen lights and the porch", False), ("", False),
])
def test_bare_replies_skip_the_cache_read(query, expected):
    assert semantic_cache.is_bare_reply(query) is expected


# --- invalidate_cache ----------------------------------------------------------------------

class _FakeRedis:
    def __init__(self, keys):
        self.keys = set(keys)
        self.scans = []
        self.unlinks = []

    async def scan_iter(self, match=None, count=None):
        import fnmatch

        self.scans.append((match, count))
        for key in sorted(self.keys):
            if fnmatch.fnmatchcase(key, match):
                yield key

    async def unlink(self, *keys):
        self.unlinks.append(len(keys))
        removed = len(self.keys & set(keys))
        self.keys -= set(keys)
        return removed


def _install(monkeypatch, client):
    monkeypatch.setattr(semantic_cache, "get_cache_client", lambda: SimpleNamespace(client=client))


def test_invalidate_cache_removes_every_semantic_key_and_nothing_else(monkeypatch):
    redis = _FakeRedis(
        [f"athena_semantic:weather_x:{n}:kv2" for n in range(1200)]
        + ["athena_semantic:dining_x:a:kv2", "conversation:abc", "athena:session:1"]
    )
    _install(monkeypatch, redis)
    assert asyncio.run(semantic_cache.invalidate_cache()) == 1201
    assert redis.keys == {"conversation:abc", "athena:session:1"}
    assert redis.scans == [("athena_semantic:*", 500)]
    assert max(redis.unlinks) <= 500 and len(redis.unlinks) == 3


def test_invalidate_cache_has_no_category_form():
    """A category isn't a key prefix (recipes are recipe_*, general answers are a hash)."""
    with pytest.raises(TypeError):
        asyncio.run(semantic_cache.invalidate_cache(category="weather"))


def test_invalidate_cache_with_a_pattern(monkeypatch):
    redis = _FakeRedis(["athena_semantic:news_x:1:kv2", "athena_semantic:news_y:1:kv2", "conversation:abc"])
    _install(monkeypatch, redis)
    assert asyncio.run(semantic_cache.invalidate_cache(pattern="athena_semantic:news_x:*")) == 1


@pytest.mark.parametrize("given,scanned", [
    ("recipe_*", "athena_semantic:recipe_*"),
    ("*", "athena_semantic:*"),
    (":weather_*", "athena_semantic:weather_*"),
    ("conversation:*", "athena_semantic:conversation:*"),
])
def test_a_pattern_is_always_kept_under_the_semantic_prefix(monkeypatch, given, scanned):
    redis = _FakeRedis(["athena_semantic:recipe_x:1:kv2", "conversation:abc", "athena:session:1"])
    _install(monkeypatch, redis)
    asyncio.run(semantic_cache.invalidate_cache(pattern=given))
    assert redis.scans == [(scanned, 500)]
    assert {"conversation:abc", "athena:session:1"} <= redis.keys


def test_invalidate_cache_is_zero_when_redis_is_down(monkeypatch):
    _install(monkeypatch, None)
    assert asyncio.run(semantic_cache.invalidate_cache()) == 0


def test_invalidate_cache_is_zero_when_the_scan_fails(monkeypatch):
    class Broken:
        async def scan_iter(self, match=None, count=None):
            raise ConnectionError("down")
            yield  # pragma: no cover

    _install(monkeypatch, Broken())
    assert asyncio.run(semantic_cache.invalidate_cache()) == 0


def test_the_cache_key_keeps_its_version_and_audience_segments():
    """Constraint (g): the campaign only adds skips; keys keep kv2, kb_ and iface_."""
    key = semantic_cache.get_cache_key(
        "weather_x", "weather today", None, "owner", None, None,
        knowledge_digest="abc123", interface_type="chat",
    )
    parts = key.split(":")
    assert semantic_cache.CACHE_KEY_VERSION == "kv2" and "kv2" in parts
    assert "kb_abc123" in parts and "mode_owner" in parts and parts[-1] == "iface_chat"


@pytest.mark.parametrize("answer", [
    "I visited their website and they open at 5.",
    "I went to the forecast service and it says rain.",
    "I went to the calendar and the game starts at 7.",
])
def test_bare_visited_or_went_to_is_no_experience_claim(answer):
    assert not grounding.experience_claim(answer, "when do they open")


@pytest.mark.parametrize("answer", [
    "I visited there last week and loved it.", "I've visited there twice.", "I ate at the new place.",
    "When I visited, the pasta was great.",
])
def test_a_visit_to_the_place_is_still_an_experience_claim(answer):
    assert grounding.experience_claim(answer, "is it good")


# --- flush_once_per_release (D24) ------------------------------------------------------------

class _FlushRedis(_FakeRedis):
    """A fake with the calls the release flush adds: exists, set(nx, ex) and delete."""

    def __init__(self, keys, *, fail_scan=False):
        super().__init__(keys)
        self.fail_scan = fail_scan
        self.set_calls = []

    async def scan_iter(self, match=None, count=None):
        if self.fail_scan:
            raise ConnectionError("down")
        async for key in super().scan_iter(match=match, count=count):
            yield key

    async def exists(self, key):
        return int(key in self.keys)

    async def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, nx, ex))
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    async def delete(self, *keys):
        self.keys -= set(keys)


ENTRIES = [f"athena_semantic:general_x:{n}:kv2" for n in range(3)] + ["conversation:abc"]


def test_the_release_flush_empties_the_cache_once_and_sets_the_marker_after(monkeypatch):
    redis = _FlushRedis(ENTRIES)
    _install(monkeypatch, redis)
    assert asyncio.run(semantic_cache.flush_once_per_release()) == 3
    assert redis.keys == {"conversation:abc", semantic_cache.FLUSH_MARKER_KEY}  # the lock is released
    redis.keys |= {"athena_semantic:general_x:new:kv2"}
    assert asyncio.run(semantic_cache.flush_once_per_release()) is None  # same release: nothing again
    assert "athena_semantic:general_x:new:kv2" in redis.keys


def test_the_marker_and_lock_keys_sit_outside_the_flushed_prefix():
    assert not semantic_cache.FLUSH_MARKER_KEY.startswith("athena_semantic:")
    assert not semantic_cache.FLUSH_LOCK_KEY.startswith("athena_semantic:")


def test_a_failed_scan_sets_no_marker_so_the_next_start_tries_again(monkeypatch):
    redis = _FlushRedis(ENTRIES, fail_scan=True)
    _install(monkeypatch, redis)
    assert asyncio.run(semantic_cache.flush_once_per_release()) is None
    assert semantic_cache.FLUSH_MARKER_KEY not in redis.keys and semantic_cache.FLUSH_LOCK_KEY not in redis.keys
    redis.fail_scan = False
    assert asyncio.run(semantic_cache.flush_once_per_release()) == 3  # positive control


def test_a_replica_that_cannot_take_the_lock_flushes_nothing(monkeypatch):
    redis = _FlushRedis(ENTRIES + [semantic_cache.FLUSH_LOCK_KEY])
    _install(monkeypatch, redis)
    assert asyncio.run(semantic_cache.flush_once_per_release()) is None
    assert redis.scans == [] and semantic_cache.FLUSH_LOCK_KEY in redis.keys  # the other replica's lock is untouched
    assert redis.set_calls[0] == (semantic_cache.FLUSH_LOCK_KEY, True, semantic_cache.FLUSH_LOCK_TTL_SECONDS)


def test_the_release_flush_does_nothing_without_redis_and_never_raises(monkeypatch):
    _install(monkeypatch, None)
    assert asyncio.run(semantic_cache.flush_once_per_release()) is None

    class Broken:
        async def exists(self, key):
            raise ConnectionError("down")

    _install(monkeypatch, Broken())
    assert asyncio.run(semantic_cache.flush_once_per_release()) is None


def test_the_lifespan_starts_the_release_flush_in_the_background():
    import ast, pathlib
    tree = ast.parse(pathlib.Path("src/orchestrator/main.py").read_text())
    lifespan = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    spawned = [
        n for n in ast.walk(lifespan) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "spawn_background"
        and any(isinstance(a, ast.Call) and getattr(a.func, "id", "") == "flush_once_per_release" for a in n.args)
    ]
    assert len(spawned) == 1


def test_an_intent_that_is_not_hashable_is_judged_by_its_value():
    from types import SimpleNamespace

    assert not grounding.ungrounded_answer("It is sunny.", SimpleNamespace(value="weather"), "weather today", False)
    assert grounding.ungrounded_answer(INVENTED, SimpleNamespace(value="dining"), "an italian place", False)  # positive control
    assert grounding.ungrounded_answer(INVENTED, "dining", "an italian place", False)

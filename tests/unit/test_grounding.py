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
    previous = {"intent": "dining", "parameters": {"tool_calls": ["search_restaurants"]}, "response": "x"}
    state = _state("do they have outdoor seating", prev_context=previous)
    assert grounding.prior_tool_context(state)
    assert grounding.forced_tool_calls(state, offered=ALL_TOOLS, search_location="Anytown") == []


@pytest.mark.parametrize("previous", [
    None,
    {"intent": "weather", "parameters": {"tool_calls": ["get_weather"]}},
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


def test_a_request_location_that_is_not_in_the_words_is_used():
    state = _state("an italian place", entities={"location": "47.6, -122.3"})
    assert grounding.resolve_search_location(state, "home") == ("47.6, -122.3", "request_entities")


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


def test_a_first_person_experience_is_ungrounded_for_any_intent():
    answer = "I've been there and the pasta is wonderful."
    assert grounding.ungrounded_answer(answer, I.GENERAL_INFO, "is it good", False)


def test_a_story_in_the_first_person_is_not_an_experience_claim():
    story = "I went to the market and I ate a pear, then I visited my grandmother."
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
    ("what is a stock split", False),
    ("what's the capital of France", False),
    ("explain how a bar chart works", False),
    ("tell me a joke", False),
    ("", False),
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


def test_invalidate_cache_by_category(monkeypatch):
    redis = _FakeRedis(["athena_semantic:weather_x:1:kv2", "athena_semantic:dining_x:1:kv2"])
    _install(monkeypatch, redis)
    assert asyncio.run(semantic_cache.invalidate_cache(category="weather")) == 1
    assert redis.keys == {"athena_semantic:dining_x:1:kv2"}
    assert redis.scans[0][0] == "athena_semantic:weather_*"


def test_invalidate_cache_with_a_pattern(monkeypatch):
    redis = _FakeRedis(["athena_semantic:news_x:1:kv2", "athena_semantic:news_y:1:kv2"])
    _install(monkeypatch, redis)
    assert asyncio.run(semantic_cache.invalidate_cache(pattern="athena_semantic:news_x:*")) == 1


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

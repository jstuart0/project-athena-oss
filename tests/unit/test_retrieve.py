"""Unit tests for orchestrator.nodes.retrieve.retrieve_node.

Covers major retrieve branches:

 1.  needs_history_context fast-path — returns conversation context, skips RAG.
 2.  needs_history_context — no prev_response/prev_query — no retrieved_data set.
 3.  WEATHER intent — happy path (VALID response, standard provider).
 4.  WEATHER intent — onecall provider selected.
 5.  WEATHER intent — forecast endpoint used when time_ref="tomorrow".
 6.  WEATHER intent — far-future request returns limitation acknowledgment.
 7.  WEATHER intent — temporal location filtered, DEFAULT_LOCATION used.
 8.  WEATHER intent — service URL not configured → _fallback_to_web_search.
 9.  WEATHER intent — RAG service call fails (Exception) → fallback.
10.  WEATHER intent — EMPTY ValidationResult → fallback.
11.  WEATHER intent — NEEDS_RETRY ValidationResult → fallback.
12.  AIRPORTS intent — happy path (VALID response).
13.  AIRPORTS intent — service URL not configured → fallback.
14.  AIRPORTS intent — RAG call fails → fallback.
15.  SPORTS intent — happy path with live game.
16.  SPORTS intent — service URL not configured → fallback.
17.  SPORTS intent — team search fails → fallback.
18.  WEBSEARCH intent — happy path, results returned.
19.  WEBSEARCH intent — prefix stripped from query before search.
20.  WEBSEARCH intent — service URL not configured → LLM knowledge fallback.
21.  WEBSEARCH intent — empty results → LLM knowledge fallback.
22.  General fallback (unknown intent) — parallel search returns results.
23.  General fallback — parallel search returns no results → LLM knowledge.
24.  No RAG data + conversation history → intent re-routed to GENERAL_INFO.
25.  Outer exception → state.error set, node_timings still populated.
26.  timing_tracker.track_sync called after node completes.
27.  node_timings["retrieve"] set on every return path, including early exit.

Patching strategy:
- rag_client / parallel_search_engine / result_fusion: install fakes via
  orchestrator.nodes._runtime.set_*() — the proxy shims delegate at call time.
- Helpers patched at their source modules:
  orchestrator.nodes.retrieve.get_weather_provider_mode
  orchestrator.nodes.retrieve.get_rag_service_url
  orchestrator.helpers._fallback_to_web_search
  orchestrator.nodes.retrieve.enhance_query_with_year
- validator patched at orchestrator.nodes.retrieve (module-level singleton).
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock, patch

# Stub heavy deps before any orchestrator import.
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

from orchestrator.nodes import retrieve_node
from orchestrator.nodes import _runtime
from orchestrator.state import OrchestratorState, IntentCategory
from orchestrator.rag_validator import ValidationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_state(
    *,
    query: str = "what is the weather?",
    intent: IntentCategory | None = IntentCategory.WEATHER,
    entities: dict | None = None,
    session_id: str | None = "sess-1",
    needs_history_context: bool = False,
    conversation_history: list | None = None,
    history_summary: str = "",
    timing_tracker=None,
) -> OrchestratorState:
    state = OrchestratorState(query=query)
    state.intent = intent
    state.entities = entities or {}
    state.session_id = session_id
    state.needs_history_context = needs_history_context
    state.conversation_history = conversation_history or []
    state.history_summary = history_summary
    state.timing_tracker = timing_tracker
    state.node_timings = {}
    state.retrieved_data = {}
    state.citations = []
    return state


def _make_rag_response(success: bool = True, data: dict | None = None, error: str | None = None):
    r = MagicMock()
    r.success = success
    r.data = data or {}
    r.error = error
    return r


def _make_rag_client(response=None):
    rc = MagicMock()
    rc.get = AsyncMock(return_value=response or _make_rag_response())
    rc.update_service_url = MagicMock()
    return rc


def _make_validator(result=ValidationResult.VALID, reason="ok", suggestions=None):
    v = MagicMock()
    v.validate_weather_response.return_value = (result, reason, suggestions or [])
    v.validate_airports_response.return_value = (result, reason, suggestions or [])
    v.validate_sports_response.return_value = (result, reason, suggestions or [])
    return v


# Pre-existing: disable strict mode so unset singletons don't warn.
_runtime.reset_for_test()


# ---------------------------------------------------------------------------
# 1-2: needs_history_context fast path
# ---------------------------------------------------------------------------

class TestHistoryContextFastPath:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_history_context_sets_conversation_context_data(self):
        state = _make_state(
            query="what team does he play for now?",
            intent=IntentCategory.SPORTS,
            needs_history_context=True,
            entities={"previous_response": "The MVP was Patrick Mahomes.", "previous_query": "who was the MVP?"},
        )
        result = _run(retrieve_node(state))
        assert result.retrieved_data["type"] == "conversation_context"
        assert result.retrieved_data["previous_response"] == "The MVP was Patrick Mahomes."
        assert result.data_source == "ConversationHistory"
        assert "Based on previous conversation" in result.citations
        assert "retrieve" in result.node_timings

    def test_history_context_no_prev_data_returns_early_no_retrieved_data(self):
        state = _make_state(
            query="what team?",
            intent=IntentCategory.SPORTS,
            needs_history_context=True,
            entities={},
        )
        result = _run(retrieve_node(state))
        # No prev_response/prev_query — retrieved_data stays empty dict
        assert not result.retrieved_data
        assert "retrieve" in result.node_timings


# ---------------------------------------------------------------------------
# 3-11: WEATHER intent
# ---------------------------------------------------------------------------

class TestWeatherIntent:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_weather_happy_path_valid_response(self):
        weather_data = {"temp": 72, "description": "sunny"}
        rc = _make_rag_client(_make_rag_response(data=weather_data))
        _runtime.set_rag_client(rc)
        v = _make_validator(ValidationResult.VALID)
        state = _make_state(query="what is the weather?")
        with (
            patch("orchestrator.nodes.retrieve.get_weather_provider_mode", new_callable=AsyncMock, return_value="standard"),
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://weather:8001"),
            patch("orchestrator.nodes.retrieve.validator", v),
        ):
            result = _run(retrieve_node(state))
        assert result.retrieved_data == weather_data
        assert result.data_source == "OpenWeatherMap"
        assert "retrieve" in result.node_timings

    def test_weather_onecall_provider_data_source(self):
        weather_data = {"temp": 68}
        rc = _make_rag_client(_make_rag_response(data=weather_data))
        _runtime.set_rag_client(rc)
        v = _make_validator(ValidationResult.VALID)
        state = _make_state(query="weather today?")
        with (
            patch("orchestrator.nodes.retrieve.get_weather_provider_mode", new_callable=AsyncMock, return_value="onecall"),
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://onecall:8002"),
            patch("orchestrator.nodes.retrieve.validator", v),
        ):
            result = _run(retrieve_node(state))
        assert result.data_source == "OpenWeatherMap OneCall 3.0"

    def test_weather_forecast_endpoint_for_tomorrow(self):
        weather_data = {"forecast": []}
        rc = _make_rag_client(_make_rag_response(data=weather_data))
        _runtime.set_rag_client(rc)
        v = _make_validator(ValidationResult.VALID)
        state = _make_state(query="weather tomorrow?", entities={"time_ref": "tomorrow", "location": "Baltimore"})
        with (
            patch("orchestrator.nodes.retrieve.get_weather_provider_mode", new_callable=AsyncMock, return_value="standard"),
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://weather:8001"),
            patch("orchestrator.nodes.retrieve.validator", v),
        ):
            result = _run(retrieve_node(state))
        # Forecast endpoint should have been called
        call_args = rc.get.call_args
        assert "/weather/forecast" in call_args[0][1]

    def test_weather_far_future_returns_limitation(self):
        rc = _make_rag_client()
        _runtime.set_rag_client(rc)
        state = _make_state(query="what will the weather be in 3 weeks?")
        with (
            patch("orchestrator.nodes.retrieve.get_weather_provider_mode", new_callable=AsyncMock, return_value="standard"),
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://weather:8001"),
        ):
            result = _run(retrieve_node(state))
        assert result.retrieved_data.get("limitation") is True
        assert result.data_source == "Weather forecast limitation"
        # RAG client should NOT have been called for the actual weather
        rc.get.assert_not_awaited()

    def test_weather_temporal_location_filtered(self):
        weather_data = {"temp": 60}
        rc = _make_rag_client(_make_rag_response(data=weather_data))
        _runtime.set_rag_client(rc)
        v = _make_validator(ValidationResult.VALID)
        # "weekend" should be filtered as temporal expression, default location used
        state = _make_state(query="weather this weekend?", entities={"location": "weekend"})
        with (
            patch("orchestrator.nodes.retrieve.get_weather_provider_mode", new_callable=AsyncMock, return_value="standard"),
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://weather:8001"),
            patch("orchestrator.nodes.retrieve.validator", v),
        ):
            result = _run(retrieve_node(state))
        # Should succeed with DEFAULT_LOCATION
        assert result.retrieved_data == weather_data

    def test_weather_service_url_not_configured_triggers_fallback(self):
        rc = _make_rag_client()
        _runtime.set_rag_client(rc)
        state = _make_state(query="weather?")
        mock_fallback = AsyncMock()
        with (
            patch("orchestrator.nodes.retrieve.get_weather_provider_mode", new_callable=AsyncMock, return_value="standard"),
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value=None),
            patch("orchestrator.nodes.retrieve._fallback_to_web_search", mock_fallback),
        ):
            _run(retrieve_node(state))
        mock_fallback.assert_awaited_once()
        assert mock_fallback.call_args[0][1] == "Weather"

    def test_weather_rag_exception_triggers_fallback(self):
        rc = MagicMock()
        rc.get = AsyncMock(side_effect=RuntimeError("connection refused"))
        rc.update_service_url = MagicMock()
        _runtime.set_rag_client(rc)
        v = _make_validator()
        state = _make_state(query="weather?")
        mock_fallback = AsyncMock()
        with (
            patch("orchestrator.nodes.retrieve.get_weather_provider_mode", new_callable=AsyncMock, return_value="standard"),
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://weather:8001"),
            patch("orchestrator.nodes.retrieve.validator", v),
            patch("orchestrator.nodes.retrieve._fallback_to_web_search", mock_fallback),
        ):
            _run(retrieve_node(state))
        mock_fallback.assert_awaited_once()

    def test_weather_empty_validation_triggers_fallback(self):
        rc = _make_rag_client(_make_rag_response(data={}))
        _runtime.set_rag_client(rc)
        v = _make_validator(ValidationResult.EMPTY, "empty data")
        state = _make_state(query="weather?")
        mock_fallback = AsyncMock()
        with (
            patch("orchestrator.nodes.retrieve.get_weather_provider_mode", new_callable=AsyncMock, return_value="standard"),
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://weather:8001"),
            patch("orchestrator.nodes.retrieve.validator", v),
            patch("orchestrator.nodes.retrieve._fallback_to_web_search", mock_fallback),
        ):
            _run(retrieve_node(state))
        mock_fallback.assert_awaited_once()

    def test_weather_needs_retry_triggers_fallback(self):
        rc = _make_rag_client(_make_rag_response(data={"partial": True}))
        _runtime.set_rag_client(rc)
        v = _make_validator(ValidationResult.NEEDS_RETRY, "missing fields")
        state = _make_state(query="weather?")
        mock_fallback = AsyncMock()
        with (
            patch("orchestrator.nodes.retrieve.get_weather_provider_mode", new_callable=AsyncMock, return_value="standard"),
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://weather:8001"),
            patch("orchestrator.nodes.retrieve.validator", v),
            patch("orchestrator.nodes.retrieve._fallback_to_web_search", mock_fallback),
        ):
            _run(retrieve_node(state))
        mock_fallback.assert_awaited_once()


# ---------------------------------------------------------------------------
# 12-14: AIRPORTS intent
# ---------------------------------------------------------------------------

class TestAirportsIntent:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_airports_happy_path(self):
        airports_data = {"flights": [{"flight": "UA123", "status": "on time"}]}
        rc = _make_rag_client(_make_rag_response(data=airports_data))
        _runtime.set_rag_client(rc)
        v = _make_validator(ValidationResult.VALID)
        state = _make_state(
            query="what flights are at BWI?",
            intent=IntentCategory.AIRPORTS,
            entities={"airport": "BWI"},
        )
        with (
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://airports:8003"),
            patch("orchestrator.nodes.retrieve.validator", v),
        ):
            result = _run(retrieve_node(state))
        assert result.retrieved_data == airports_data
        assert result.data_source == "FlightAware"
        assert any("BWI" in c for c in result.citations)

    def test_airports_service_url_not_configured_triggers_fallback(self):
        rc = _make_rag_client()
        _runtime.set_rag_client(rc)
        state = _make_state(query="flights at BWI?", intent=IntentCategory.AIRPORTS)
        mock_fallback = AsyncMock()
        with (
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value=None),
            patch("orchestrator.nodes.retrieve._fallback_to_web_search", mock_fallback),
        ):
            _run(retrieve_node(state))
        mock_fallback.assert_awaited_once()
        assert mock_fallback.call_args[0][1] == "Airports"

    def test_airports_rag_failure_triggers_fallback(self):
        rc = MagicMock()
        rc.get = AsyncMock(side_effect=RuntimeError("timeout"))
        rc.update_service_url = MagicMock()
        _runtime.set_rag_client(rc)
        state = _make_state(query="flights at DCA?", intent=IntentCategory.AIRPORTS)
        mock_fallback = AsyncMock()
        with (
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://airports:8003"),
            patch("orchestrator.nodes.retrieve._fallback_to_web_search", mock_fallback),
        ):
            _run(retrieve_node(state))
        mock_fallback.assert_awaited_once()


# ---------------------------------------------------------------------------
# 15-17: SPORTS intent
# ---------------------------------------------------------------------------

class TestSportsIntent:
    def setup_method(self):
        _runtime.reset_for_test()

    def _make_sports_rc(self, team_data=None, last=None, next_ev=None, live=None):
        search_resp = _make_rag_response(data={"teams": [team_data or {"idTeam": "133602", "strTeam": "Ravens", "strLeague": "football/nfl"}]})
        last_resp = _make_rag_response(data={"events": last or []})
        next_resp = _make_rag_response(data={"events": next_ev or []})
        live_resp = _make_rag_response(data={"games": live or []})
        rc = MagicMock()
        rc.update_service_url = MagicMock()
        # Return different responses based on call order
        rc.get = AsyncMock(side_effect=[search_resp, last_resp, next_resp, live_resp])
        return rc

    def test_sports_happy_path_with_live_game(self):
        live_games = [{"home_team": "Ravens", "away_team": "Chiefs", "home_score": 14, "away_score": 7, "status": "Q2"}]
        rc = self._make_sports_rc(live=live_games)
        _runtime.set_rag_client(rc)
        v = _make_validator(ValidationResult.VALID)
        state = _make_state(
            query="how are the Ravens doing?",
            intent=IntentCategory.SPORTS,
            entities={"team": "Ravens"},
        )
        with (
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://sports:8004"),
            patch("orchestrator.nodes.retrieve.validator", v),
        ):
            result = _run(retrieve_node(state))
        assert result.retrieved_data["team"] == "Ravens"
        assert result.retrieved_data["has_live_game"] is True
        assert result.data_source == "TheSportsDB"

    def test_sports_service_url_not_configured_triggers_fallback(self):
        rc = _make_rag_client()
        _runtime.set_rag_client(rc)
        state = _make_state(query="Ravens score?", intent=IntentCategory.SPORTS)
        mock_fallback = AsyncMock()
        with (
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value=None),
            patch("orchestrator.nodes.retrieve._fallback_to_web_search", mock_fallback),
        ):
            _run(retrieve_node(state))
        mock_fallback.assert_awaited_once()
        assert mock_fallback.call_args[0][1] == "Sports"

    def test_sports_team_search_fails_triggers_fallback(self):
        rc = MagicMock()
        rc.update_service_url = MagicMock()
        rc.get = AsyncMock(return_value=_make_rag_response(success=False, error="404 not found"))
        _runtime.set_rag_client(rc)
        state = _make_state(query="how are the Ravens?", intent=IntentCategory.SPORTS, entities={"team": "Ravens"})
        mock_fallback = AsyncMock()
        with (
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://sports:8004"),
            patch("orchestrator.nodes.retrieve._fallback_to_web_search", mock_fallback),
        ):
            _run(retrieve_node(state))
        mock_fallback.assert_awaited_once()


# ---------------------------------------------------------------------------
# 18-21: WEBSEARCH intent
# ---------------------------------------------------------------------------

class TestWebsearchIntent:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_websearch_happy_path_returns_results(self):
        results = [{"title": "Page 1", "url": "https://example.com", "snippet": "info"}]
        search_data = {"results": results}
        rc = _make_rag_client(_make_rag_response(data=search_data))
        _runtime.set_rag_client(rc)
        state = _make_state(query="search the web for python tutorials", intent=IntentCategory.WEBSEARCH)
        with patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://websearch:8018"):
            result = _run(retrieve_node(state))
        assert result.retrieved_data["source"] == "brave_search"
        assert result.data_source == "Brave Search"
        assert result.retrieved_data["total_results"] == 1

    def test_websearch_prefix_stripped(self):
        results = [{"title": "Python", "url": "https://python.org", "snippet": "info"}]
        rc = _make_rag_client(_make_rag_response(data={"results": results}))
        _runtime.set_rag_client(rc)
        state = _make_state(query="search the web for python tutorials", intent=IntentCategory.WEBSEARCH)
        with patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://websearch:8018"):
            result = _run(retrieve_node(state))
        # retrieved_data["query"] should have prefix stripped
        assert result.retrieved_data["query"] == "python tutorials"

    def test_websearch_no_service_url_falls_back_to_llm_knowledge(self):
        rc = _make_rag_client()
        _runtime.set_rag_client(rc)
        state = _make_state(query="search the web for news", intent=IntentCategory.WEBSEARCH)
        with (
            patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value=None),
            patch("orchestrator.nodes.retrieve.WEBSEARCH_SERVICE_URL", "http://localhost:8018"),
        ):
            result = _run(retrieve_node(state))
        assert result.retrieved_data == {}
        assert "LLM knowledge" in result.data_source

    def test_websearch_empty_results_falls_back_to_llm_knowledge(self):
        rc = _make_rag_client(_make_rag_response(data={"results": []}))
        _runtime.set_rag_client(rc)
        state = _make_state(query="search the web for obscure topic", intent=IntentCategory.WEBSEARCH)
        with patch("orchestrator.nodes.retrieve.get_rag_service_url", new_callable=AsyncMock, return_value="http://websearch:8018"):
            result = _run(retrieve_node(state))
        assert result.retrieved_data == {}
        assert "no web results" in result.data_source


# ---------------------------------------------------------------------------
# 22-23: General fallback (unknown intent → parallel search)
# ---------------------------------------------------------------------------

class TestParallelSearchFallback:
    def setup_method(self):
        _runtime.reset_for_test()

    def _make_search_result(self, source="google"):
        r = MagicMock()
        r.source = source
        r.to_dict.return_value = {"title": "result", "url": "https://example.com"}
        return r

    def test_parallel_search_returns_fused_results(self):
        pse = MagicMock()
        fused = [self._make_search_result("brave"), self._make_search_result("google")]
        pse.search = AsyncMock(return_value=("general", [MagicMock(), MagicMock()]))
        _runtime.set_parallel_search_engine(pse)

        rf = MagicMock()
        rf.get_top_results.return_value = fused
        _runtime.set_result_fusion(rf)

        state = _make_state(query="who invented the telephone?", intent=IntentCategory.GENERAL_INFO)
        with patch("orchestrator.nodes.retrieve.enhance_query_with_year", return_value="who invented the telephone? 2024"):
            result = _run(retrieve_node(state))

        assert result.retrieved_data["intent"] == "general"
        assert result.retrieved_data["fused_results"] == 2
        assert "brave" in result.retrieved_data["sources"] or "google" in result.retrieved_data["sources"]

    def test_parallel_search_no_results_uses_llm_knowledge(self):
        pse = MagicMock()
        pse.search = AsyncMock(return_value=("general", []))
        _runtime.set_parallel_search_engine(pse)

        state = _make_state(query="who invented the telephone?", intent=IntentCategory.GENERAL_INFO)
        with patch("orchestrator.nodes.retrieve.enhance_query_with_year", return_value="who invented the telephone? 2024"):
            result = _run(retrieve_node(state))

        assert result.retrieved_data == {}
        assert result.data_source == "LLM knowledge"


# ---------------------------------------------------------------------------
# 24: Reroute to GENERAL_INFO when no RAG data + conversation context
# ---------------------------------------------------------------------------

class TestRerouteToGeneralInfo:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_no_rag_data_with_history_reroutes_to_general_info(self):
        pse = MagicMock()
        pse.search = AsyncMock(return_value=("general", []))
        _runtime.set_parallel_search_engine(pse)

        state = _make_state(
            query="what city was that restaurant in?",
            intent=IntentCategory.DINING,
            conversation_history=[{"role": "user", "content": "tell me about a restaurant"}],
        )
        with patch("orchestrator.nodes.retrieve.enhance_query_with_year", return_value="what city? 2024"):
            result = _run(retrieve_node(state))

        assert result.intent == IntentCategory.GENERAL_INFO

    def test_no_rag_data_without_history_does_not_reroute(self):
        pse = MagicMock()
        pse.search = AsyncMock(return_value=("general", []))
        _runtime.set_parallel_search_engine(pse)

        state = _make_state(
            query="what city was that restaurant in?",
            intent=IntentCategory.DINING,
            conversation_history=[],
            history_summary="",
        )
        with patch("orchestrator.nodes.retrieve.enhance_query_with_year", return_value="what city? 2024"):
            result = _run(retrieve_node(state))

        # Intent stays DINING — no conversation history to justify reroute
        assert result.intent == IntentCategory.DINING


# ---------------------------------------------------------------------------
# 25-27: Error handling and metrics
# ---------------------------------------------------------------------------

class TestErrorHandlingAndMetrics:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_outer_exception_sets_error_and_node_timings(self):
        pse = MagicMock()
        pse.search = AsyncMock(side_effect=RuntimeError("search engine down"))
        _runtime.set_parallel_search_engine(pse)

        state = _make_state(query="anything", intent=IntentCategory.GENERAL_INFO)
        with patch("orchestrator.nodes.retrieve.enhance_query_with_year", return_value="anything 2024"):
            result = _run(retrieve_node(state))

        assert "Retrieval failed" in (result.error or "")
        assert "retrieve" in result.node_timings
        assert isinstance(result.node_timings["retrieve"], float)

    def test_timing_tracker_called(self):
        pse = MagicMock()
        pse.search = AsyncMock(return_value=("general", []))
        _runtime.set_parallel_search_engine(pse)

        tracker = MagicMock()
        state = _make_state(query="anything", intent=IntentCategory.GENERAL_INFO, timing_tracker=tracker)
        with patch("orchestrator.nodes.retrieve.enhance_query_with_year", return_value="anything 2024"):
            _run(retrieve_node(state))

        tracker.track_sync.assert_called_once_with("graph", "retrieve", mock.ANY)

    def test_node_timings_set_on_early_return_path(self):
        """needs_history_context exits early — node_timings["retrieve"] must still be set."""
        state = _make_state(needs_history_context=True, entities={})
        result = _run(retrieve_node(state))
        assert isinstance(result.node_timings.get("retrieve"), float)

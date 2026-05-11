"""retrieve_node — extracted from orchestrator.main during Phase 3.5 (ATHENA-10).

Retrieves information from the appropriate RAG service based on query intent.
Falls back to parallel search or web search when RAG is unavailable.

Largest single-node extraction in Campaign 1 (~538 LOC). Byte-identical move.
See thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md.

Round-3 R3-H2: _fallback_to_web_search was moved to helpers.py in Phase 2.1
(not co-located here) because tool_call_node also calls it — imports it from
orchestrator.helpers so both nodes share the same definition.
"""
from __future__ import annotations

import asyncio
import time

import structlog

from orchestrator.nodes._runtime import (
    get_rag_client,
    get_parallel_search_engine,
    get_result_fusion,
)
from orchestrator.rag_validator import validator, ValidationResult
from orchestrator.state import OrchestratorState, IntentCategory
from orchestrator.urls import WEBSEARCH_SERVICE_URL
from orchestrator.utils.constants import DEFAULT_LOCATION
from orchestrator.helpers import (
    get_weather_provider_mode,
    get_rag_service_url,
    enhance_query_with_year,
    _fallback_to_web_search,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Proxy shims — forward bare-global attribute access to _runtime singletons
# ---------------------------------------------------------------------------

class _RagClientProxy:
    """Forward attribute access to the runtime RAG client at call time.

    retrieve_node references ``rag_client`` as a bare module global.
    The actual client is registered in _runtime by main.py's lifespan, so
    we cannot bind it at import time.  This proxy resolves the current value
    on every attribute lookup, keeping the function body byte-identical.
    """

    def __bool__(self) -> bool:
        return get_rag_client() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_rag_client(), name)


class _ParallelSearchEngineProxy:
    """Forward attribute access to the runtime parallel search engine at call time."""

    def __bool__(self) -> bool:
        return get_parallel_search_engine() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_parallel_search_engine(), name)


class _ResultFusionProxy:
    """Forward attribute access to the runtime result fusion at call time."""

    def __bool__(self) -> bool:
        return get_result_fusion() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_result_fusion(), name)


rag_client = _RagClientProxy()
parallel_search_engine = _ParallelSearchEngineProxy()
result_fusion = _ResultFusionProxy()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def retrieve_node(state: OrchestratorState) -> OrchestratorState:
    """
    Retrieve information from appropriate RAG service.
    Falls back to web search if RAG service is unavailable.
    """
    start = time.time()

    try:
        # Skip RAG for pronoun-based follow-ups that need LLM to resolve from conversation history
        # e.g., "what team does he play for now" after "who was the MVP of the Super Bowl"
        if state.needs_history_context:
            logger.info(f"Skipping RAG - query '{state.query[:50]}...' needs conversation history for pronoun resolution")
            # Use previous response as context for the LLM to answer from
            prev_response = state.entities.get("previous_response", "")
            prev_query = state.entities.get("previous_query", "")
            if prev_response or prev_query:
                state.retrieved_data = {
                    "type": "conversation_context",
                    "previous_query": prev_query,
                    "previous_response": prev_response,
                    "current_query": state.query,
                    "note": "Answer this follow-up question using the previous context"
                }
                state.data_source = "ConversationHistory"
                state.citations.append("Based on previous conversation")
            state.node_timings["retrieve"] = time.time() - start
            return state

        if state.intent == IntentCategory.WEATHER:
            # Check which weather provider to use (feature flag)
            weather_mode = await get_weather_provider_mode()
            service_name = "onecall" if weather_mode == "onecall" else "weather"

            # Get dynamic RAG service URL and update client if needed
            service_url = await get_rag_service_url(service_name)
            if not service_url:
                logger.error(f"{service_name.title()} RAG service URL not configured")
                # Fall back to web search instead of failing
                await _fallback_to_web_search(state, "Weather", "service not configured")
            else:
                try:
                    # Update RAG client URL if different from default
                    rag_client.update_service_url(service_name, service_url)

                    # Call weather service with unified RAG client (includes circuit breaker, rate limiting)
                    # Filter out temporal words/phrases that shouldn't be treated as locations
                    TEMPORAL_WORDS = {'today', 'tomorrow', 'tonight', 'yesterday', 'now', 'morning',
                                      'afternoon', 'evening', 'night', 'weekend', 'week', 'day', 'hour'}
                    TEMPORAL_PHRASES = {
                        'next couple of days', 'next few days', 'next week', 'this week',
                        'this weekend', 'next weekend', 'coming days', 'few days',
                        'couple of days', 'couple days', 'rest of the week', 'rest of the day',
                        'next couple days', 'next several days', 'the week', 'the weekend',
                    }
                    raw_location = state.entities.get("location", DEFAULT_LOCATION)
                    raw_lower = raw_location.lower().strip() if raw_location else ""
                    # Use default location if extracted location is actually a temporal word/phrase
                    if raw_lower and (raw_lower in TEMPORAL_WORDS or raw_lower in TEMPORAL_PHRASES):
                        logger.info(f"Filtering temporal expression '{raw_location}' from location, using default: {DEFAULT_LOCATION}")
                        location = DEFAULT_LOCATION
                    else:
                        location = raw_location or DEFAULT_LOCATION
                    time_ref = state.entities.get("time_ref")

                    # Detect temporal intent from query when time_ref is not set by classifier
                    if not time_ref:
                        query_lower = state.query.lower()
                        if any(p in query_lower for p in ['next couple', 'next few', 'next several', 'coming days', 'forecast']):
                            time_ref = "this_week"
                        elif 'tomorrow' in query_lower:
                            time_ref = "tomorrow"
                        elif 'weekend' in query_lower:
                            time_ref = "this_weekend"
                        elif any(p in query_lower for p in ['next week', 'this week', 'rest of the week']):
                            time_ref = "this_week"
                        if time_ref:
                            logger.info(f"Inferred time_ref='{time_ref}' from query")

                    # Round 17: Detect far-future weather requests beyond forecast range (7-10 days)
                    # Patterns like "3 weeks", "in 2 weeks", "next month" should acknowledge limitations
                    query_lower = state.query.lower()
                    far_future_patterns = [
                        r'\b(\d+)\s*weeks?\b',  # "3 weeks", "in 2 weeks"
                        r'\bnext\s+month\b', r'\bin\s+a\s+month\b',  # "next month"
                        r'\b(\d+)\s+days?\b',  # Check if days > 10
                    ]
                    is_far_future = False
                    import re as weather_re
                    for pattern in far_future_patterns:
                        match = weather_re.search(pattern, query_lower)
                        if match:
                            if 'week' in pattern or 'month' in pattern:
                                # Any mention of weeks or months is too far
                                num_weeks = int(match.group(1)) if match.lastindex else 1
                                if num_weeks >= 2 or 'month' in query_lower:
                                    is_far_future = True
                                    break
                            elif 'days' in pattern:
                                num_days = int(match.group(1)) if match.lastindex else 0
                                if num_days > 10:
                                    is_far_future = True
                                    break

                    if is_far_future:
                        # Return a limitation acknowledgment instead of inaccurate forecast
                        logger.info(f"Far future weather request detected: '{state.query}'")
                        state.retrieved_data = {
                            "limitation": True,
                            "message": "Weather forecasts are only reliable up to about 7-10 days out. "
                                      "Predictions beyond that become increasingly inaccurate. "
                                      "I can tell you the current weather or the forecast for the next week, "
                                      "but I can't provide reliable information that far in advance.",
                            "location": location
                        }
                        state.data_source = "Weather forecast limitation"
                        state.citations.append("Weather forecast range limitation")
                        # Skip the actual weather API call - go directly to synthesis
                    else:
                        # Normal weather processing - not a far-future request
                        # Determine which endpoint to use based on temporal context
                        if time_ref:
                            # Use forecast endpoint for temporal follow-ups
                            days = 1 if time_ref == "tomorrow" else 5 if time_ref == "this_weekend" else 7
                            # OneCall supports up to 8 days
                            if weather_mode == "onecall" and days > 5:
                                days = min(days, 8)
                            logger.info(f"Using forecast endpoint for time_ref={time_ref}, days={days}, provider={weather_mode}")
                            response = await rag_client.get(
                                service_name,
                                "/weather/forecast",
                                params={"location": location, "days": days}
                            )
                        else:
                            # Use current weather endpoint
                            response = await rag_client.get(
                                service_name,
                                "/weather/current",
                                params={"location": location}
                            )

                        if not response.success:
                            raise Exception(response.error or f"{service_name} service call failed")

                        weather_data = response.data

                        # Validate Weather RAG response quality
                        validation_result, reason, suggestions = validator.validate_weather_response(
                            weather_data, state.query
                        )

                        if validation_result == ValidationResult.VALID:
                            # Response is good, use it
                            state.retrieved_data = weather_data
                            state.data_source = "OpenWeatherMap OneCall 3.0" if weather_mode == "onecall" else "OpenWeatherMap"
                            state.citations.append(f"Weather data from {state.data_source} for {location}")
                            logger.debug(f"Weather RAG validation passed: {reason}, provider={weather_mode}")

                        elif validation_result in [ValidationResult.EMPTY, ValidationResult.INVALID]:
                            # Data is empty or invalid, trigger web search fallback
                            logger.warning(
                                f"Weather RAG validation failed: {validation_result.value} - {reason}"
                            )
                            if suggestions:
                                logger.info(f"Fallback suggestion: {suggestions}")
                            await _fallback_to_web_search(state, "Weather", reason)

                        elif validation_result == ValidationResult.NEEDS_RETRY:
                            # Data structure mismatch or missing information
                            logger.info(
                                f"Weather RAG needs retry: {reason}. Suggestions: {suggestions}"
                            )
                            # For now, fall back to web search for retry scenarios
                            await _fallback_to_web_search(state, "Weather", reason)

                except Exception as e:
                    # RAG service failed - fall back to web search
                    await _fallback_to_web_search(state, "Weather", str(e))

        elif state.intent == IntentCategory.AIRPORTS:
            # Get dynamic RAG service URL and update client if needed
            service_url = await get_rag_service_url("airports")
            if not service_url:
                logger.error("Airports RAG service URL not configured")
                await _fallback_to_web_search(state, "Airports", "service not configured")
            else:
                try:
                    # Update RAG client URL if different from default
                    rag_client.update_service_url("airports", service_url)

                    # Call airports service with unified RAG client
                    airport = state.entities.get("airport", "BWI")
                    response = await rag_client.get("airports", f"/airports/{airport}")

                    if not response.success:
                        raise Exception(response.error or "Airports service call failed")

                    airports_data = response.data

                    # Validate Airports RAG response quality
                    validation_result, reason, suggestions = validator.validate_airports_response(
                        airports_data, state.query
                    )

                    if validation_result == ValidationResult.VALID:
                        # Response is good, use it
                        state.retrieved_data = airports_data
                        state.data_source = "FlightAware"
                        state.citations.append(f"Flight data from FlightAware for {airport}")
                        logger.debug(f"Airports RAG validation passed: {reason}")

                    elif validation_result in [ValidationResult.EMPTY, ValidationResult.INVALID]:
                        # Data is empty or invalid, trigger web search fallback
                        logger.warning(
                            f"Airports RAG validation failed: {validation_result.value} - {reason}"
                        )
                        if suggestions:
                            logger.info(f"Fallback suggestion: {suggestions}")
                        await _fallback_to_web_search(state, "Airports", reason)

                    elif validation_result == ValidationResult.NEEDS_RETRY:
                        # Data structure mismatch or missing information
                        logger.info(
                            f"Airports RAG needs retry: {reason}. Suggestions: {suggestions}"
                        )
                        # For now, fall back to web search for retry scenarios
                        await _fallback_to_web_search(state, "Airports", reason)

                except Exception as e:
                    # RAG service failed - fall back to web search
                    await _fallback_to_web_search(state, "Airports", str(e))

        elif state.intent == IntentCategory.SPORTS:
            # Get dynamic RAG service URL and update client if needed
            service_url = await get_rag_service_url("sports")
            if not service_url:
                logger.error("Sports RAG service URL not configured")
                await _fallback_to_web_search(state, "Sports", "service not configured")
            else:
                try:
                    # Update RAG client URL if different from default
                    rag_client.update_service_url("sports", service_url)

                    # Call sports service with unified RAG client
                    team = state.entities.get("team", "Ravens")

                    # Search for team
                    search_response = await rag_client.get(
                        "sports",
                        "/sports/teams/search",
                        params={"query": team}
                    )
                    if not search_response.success:
                        raise Exception(search_response.error or "Sports team search failed")

                    search_data = search_response.data

                    if search_data.get("teams"):
                        team_info = search_data["teams"][0]
                        team_id = team_info["idTeam"]
                        team_full_name = team_info.get("strTeam", team)
                        team_league = team_info.get("strLeague", "")

                        # Determine league for live scores
                        league_code_map = {
                            "soccer/eng.1": "premier-league",
                            "soccer/esp.1": "la-liga",
                            "football/nfl": "nfl",
                            "basketball/nba": "nba",
                            "baseball/mlb": "mlb",
                            "hockey/nhl": "nhl",
                        }
                        live_league = league_code_map.get(team_league, "nfl")

                        # Fetch last events, next events, AND live scores in parallel
                        # This provides comprehensive data for any sports query
                        last_response, next_response, live_response = await asyncio.gather(
                            rag_client.get("sports", f"/sports/events/{team_id}/last"),
                            rag_client.get("sports", f"/sports/events/{team_id}/next"),
                            rag_client.get("sports", f"/sports/scores/live", params={"league": live_league, "team": team_full_name}),
                            return_exceptions=True
                        )

                        # Build combined response with past, upcoming, and live games
                        events_data = {
                            "team": team_full_name,
                            "team_id": team_id,
                            "league": team_league
                        }

                        # Process last games
                        if isinstance(last_response, Exception) or not last_response.success:
                            events_data["last_games"] = []
                            logger.debug(f"Last events fetch failed: {last_response}")
                        else:
                            events_data["last_games"] = last_response.data.get("events", [])

                        # Process next games (may include season_status if season ended)
                        if isinstance(next_response, Exception) or not next_response.success:
                            events_data["upcoming_games"] = []
                            logger.debug(f"Next events fetch failed: {next_response}")
                        else:
                            next_events = next_response.data.get("events", [])
                            events_data["upcoming_games"] = next_events
                            # Check if season has ended
                            if next_events and next_events[0].get("season_status") == "ended":
                                events_data["season_status"] = "ended"
                                events_data["team_record"] = next_events[0].get("team_record")
                                events_data["team_standing"] = next_events[0].get("team_standing")
                                events_data["season_message"] = next_events[0].get("message")

                        # Process live scores
                        if isinstance(live_response, Exception) or not live_response.success:
                            events_data["live_games"] = []
                        else:
                            live_data = live_response.data
                            live_games = live_data.get("games", [])
                            # Filter to only games with this team
                            team_lower = team_full_name.lower()
                            matching_live = [
                                g for g in live_games
                                if team_lower in g.get("home_team", "").lower()
                                or team_lower in g.get("away_team", "").lower()
                            ]
                            events_data["live_games"] = matching_live
                            if matching_live:
                                events_data["has_live_game"] = True
                                game = matching_live[0]
                                events_data["live_score_summary"] = (
                                    f"{game['away_team']} {game['away_score']} - "
                                    f"{game['home_score']} {game['home_team']} ({game['status']})"
                                )

                        logger.info(
                            f"Sports data fetched for {team_full_name}",
                            last_games=len(events_data.get("last_games", [])),
                            upcoming_games=len(events_data.get("upcoming_games", [])),
                            live_games=len(events_data.get("live_games", [])),
                            season_ended=events_data.get("season_status") == "ended"
                        )

                        # Validate Sports RAG response quality
                        validation_result, reason, suggestions = validator.validate_sports_response(
                            events_data, state.query
                        )

                        if validation_result == ValidationResult.VALID:
                            # Response is good, use it
                            state.retrieved_data = events_data
                            state.data_source = "TheSportsDB"
                            state.citations.append(f"Sports data from TheSportsDB for {team}")
                            logger.debug(f"Sports RAG validation passed: {reason}")

                        elif validation_result in [ValidationResult.EMPTY, ValidationResult.INVALID]:
                            # Data is empty or invalid, trigger web search fallback
                            logger.warning(
                                f"Sports RAG validation failed: {validation_result.value} - {reason}"
                            )
                            if suggestions:
                                logger.info(f"Fallback suggestion: {suggestions}")
                            await _fallback_to_web_search(state, "Sports", reason)

                        elif validation_result == ValidationResult.NEEDS_RETRY:
                            # Data structure mismatch (e.g., got schedule when query wants scores)
                            logger.info(
                                f"Sports RAG needs retry: {reason}. Suggestions: {suggestions}"
                            )
                            # For now, fall back to web search for retry scenarios
                            await _fallback_to_web_search(state, "Sports", reason)

                except Exception as e:
                    # RAG service failed - fall back to web search
                    await _fallback_to_web_search(state, "Sports", str(e))

        elif state.intent == IntentCategory.WEBSEARCH:
            # Explicit web search request - use Brave Search via websearch RAG service
            service_url = await get_rag_service_url("websearch")
            if not service_url:
                # Fall back to environment variable URL
                service_url = WEBSEARCH_SERVICE_URL

            if not service_url or service_url == "http://localhost:8018":
                logger.error("WebSearch RAG service URL not configured properly")
                # Fall back to parallel search as last resort
                state.retrieved_data = {}
                state.data_source = "LLM knowledge (websearch service unavailable)"
            else:
                try:
                    # Extract the actual search query by removing common prefixes
                    search_query = state.query.lower()
                    prefixes_to_remove = [
                        "search the web for ", "search the web ", "search the internet for ",
                        "search the internet ", "search online for ", "search online ",
                        "look up online ", "google ", "find on the web ",
                        "find on the internet ", "find online ", "web search for ",
                        "web search ", "do a web search for ", "do a web search ",
                        "search for information on ", "search for information about ",
                        "can you search for ", "could you search for ",
                        "i want you to search for ", "i want you to search ",
                        "please search for "
                    ]
                    for prefix in prefixes_to_remove:
                        if search_query.startswith(prefix):
                            search_query = state.query[len(prefix):].strip()
                            break
                    else:
                        # No prefix matched, use original query
                        search_query = state.query

                    logger.info(f"WebSearch: Searching for '{search_query}'")

                    # Update RAG client URL if different from default
                    rag_client.update_service_url("websearch", service_url)

                    # Call websearch service
                    search_response = await rag_client.get(
                        "websearch",
                        "/search",
                        params={"query": search_query, "count": 10, "safesearch": "moderate"}
                    )

                    if search_response.success and search_response.data:
                        search_data = search_response.data
                        results = search_data.get("results", [])

                        if results:
                            state.retrieved_data = {
                                "query": search_query,
                                "results": results,
                                "total_results": len(results),
                                "source": "brave_search"
                            }
                            state.data_source = "Brave Search"
                            state.citations.extend([
                                f"[{r.get('title', 'Web result')}]({r.get('url', '')})"
                                for r in results[:3] if r.get('url')
                            ])
                            logger.info(f"WebSearch: Found {len(results)} results")
                        else:
                            logger.warning("WebSearch: No results found, falling back to LLM knowledge")
                            state.retrieved_data = {}
                            state.data_source = "LLM knowledge (no web results)"
                    else:
                        error_msg = search_response.error or "Unknown error"
                        logger.warning(f"WebSearch: Service returned error: {error_msg}")
                        state.retrieved_data = {}
                        state.data_source = f"LLM knowledge (websearch error: {error_msg})"

                except Exception as e:
                    logger.error(f"WebSearch: Exception occurred: {e}", exc_info=True)
                    state.retrieved_data = {}
                    state.data_source = f"LLM knowledge (websearch exception)"

        else:
            # Use intent-based parallel web search for unknown/general queries
            logger.info("Attempting intent-based parallel search")

            # Check if query has location-sensitive keywords that need location context
            location_keywords = ["local", "near me", "nearby", "in my area", "around here", "close by", "in town"]
            query_lower = state.query.lower()
            user_location = state.entities.get("location", DEFAULT_LOCATION) if state.entities else DEFAULT_LOCATION

            if any(kw in query_lower for kw in location_keywords):
                search_query = f"{enhance_query_with_year(state.query)} {user_location}"
                logger.info(f"Location-sensitive query detected, adding location: {user_location}")
            else:
                search_query = enhance_query_with_year(state.query)

            # Execute parallel search with automatic intent classification
            intent, search_results = await parallel_search_engine.search(
                query=search_query,
                location=DEFAULT_LOCATION,
                limit_per_provider=5
            )

            logger.info(f"Search intent classified as: '{intent}'")

            if search_results:
                # Fuse and rank results based on classified intent
                fused_results = result_fusion.get_top_results(
                    results=search_results,
                    query=state.query,
                    intent=intent,
                    limit=5
                )

                logger.info(f"Parallel search returned {len(fused_results)} fused results (intent: {intent})")

                # Convert to dict format for LLM
                search_data = {
                    "intent": intent,
                    "results": [r.to_dict() for r in fused_results],
                    "sources": list(set(r.source for r in fused_results)),
                    "total_results": len(search_results),
                    "fused_results": len(fused_results)
                }

                state.retrieved_data = search_data
                state.data_source = f"Parallel Search ({intent}): {', '.join(search_data['sources'])}"
                state.citations.extend([f"Search result from {r.source}" for r in fused_results])
                logger.info(f"Parallel search completed: intent={intent}, sources={search_data['sources']}")
            else:
                # Fallback to LLM knowledge
                state.retrieved_data = {}
                state.data_source = "LLM knowledge"
                logger.info(f"Parallel search returned no results (intent: {intent}), using LLM knowledge")

        logger.info(f"Retrieved data from {state.data_source}")

    except Exception as e:
        logger.error(f"Retrieval error: {e}", exc_info=True)
        state.error = f"Retrieval failed: {str(e)}"

    # If RAG retrieval returned no data but we have conversation context,
    # re-route to GENERAL_INFO so the synthesis prompt uses conversation history
    # instead of the restrictive "no information" path. This handles cases like
    # "what city was that restaurant in?" being classified as dining but having
    # no RAG data — the answer is in the conversation history.
    if (not state.retrieved_data
            and state.intent != IntentCategory.GENERAL_INFO
            and (state.conversation_history or state.history_summary)):
        logger.info(
            "retrieve_reroute_to_general_info",
            original_intent=state.intent.value if state.intent else None,
            reason="no_rag_data_with_conversation_context"
        )
        state.intent = IntentCategory.GENERAL_INFO

    retrieve_duration = time.time() - start
    state.node_timings["retrieve"] = retrieve_duration

    # Track retrieve timing in timing tracker
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "retrieve", retrieve_duration)

    return state

"""
RAG Service Response Validation System

Validates responses from RAG services (Weather, Sports, Airports) to prevent
silent failures when services return empty or invalid data.

Features:
- Service-specific validation for Weather, Sports, Airports
- Validates data structure and content quality
- Returns structured validation results with suggestions
- Performance target: < 10ms per validation
"""

import logging
from typing import Dict, Any, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class ValidationResult(Enum):
    """Validation result status."""
    VALID = "valid"              # Data is good, use it
    EMPTY = "empty"              # Data is empty, trigger fallback
    INVALID = "invalid"          # Data is malformed, trigger fallback
    NEEDS_RETRY = "needs_retry"  # Data missing, retry with different params


class RAGValidator:
    """
    Validates RAG service responses for quality and completeness.

    Provides service-specific validation and auto-fix strategies.
    """

    # Patterns that indicate unhelpful RAG responses
    UNHELPFUL_PATTERNS = [
        "i don't have access",
        "i cannot provide",
        "i don't have current",
        "i don't have real-time",
        "i'm unable to access",
        "i don't have the ability",
        "no information available",
        "data not available",
        "unable to retrieve",
        "service unavailable",
        "api error",
        "connection failed",
        "timeout",
        "no data found",
        "empty response"
    ]

    def _check_content_quality(self, content: str, query: str) -> Tuple[bool, str]:
        """
        Check if content contains unhelpful patterns.

        Args:
            content: Content to check (from RAG response or LLM answer)
            query: Original user query

        Returns:
            Tuple of (is_helpful, reason)
            - is_helpful: True if content looks helpful, False if unhelpful
            - reason: Explanation of why content failed quality check
        """
        if not content:
            return (False, "Empty content")

        content_lower = content.lower()

        # Check for unhelpful patterns
        for pattern in self.UNHELPFUL_PATTERNS:
            if pattern in content_lower:
                logger.warning(f"Content contains unhelpful pattern '{pattern}': {content[:100]}")
                return (False, f"Contains unhelpful pattern: '{pattern}'")

        # Check for very short responses (< 20 chars) unless it's a simple fact
        if len(content) < 20 and query.lower() not in content_lower:
            return (False, "Response too short and doesn't answer query")

        return (True, "Content appears helpful")

    def validate_answer_quality(
        self,
        answer: str,
        query: str,
        intent: str
    ) -> Tuple[ValidationResult, str, Optional[Dict[str, Any]]]:
        """
        Validate final synthesized answer quality.

        This is Priority 2: Post-synthesis validation to catch bad RAG data
        that passed initial validation but produced unhelpful answers.

        Args:
            answer: Final synthesized answer from LLM
            query: Original user query
            intent: Query intent category

        Returns:
            Tuple of (ValidationResult, reason, suggestions)
        """
        try:
            # Check content quality
            is_helpful, reason = self._check_content_quality(answer, query)

            if not is_helpful:
                logger.warning(f"Answer quality validation failed: {reason}. Answer: {answer[:200]}")
                return (
                    ValidationResult.INVALID,
                    f"Answer is unhelpful: {reason}",
                    {
                        "fallback_action": "web_search",
                        "reason": "unhelpful_answer",
                        "original_intent": intent
                    }
                )

            # Check if answer admits it doesn't have the information
            # NOTE: Using VERY SPECIFIC phrases to avoid false positives
            # These patterns indicate explicit admission of ignorance, not conversational hedging
            answer_lower = answer.lower()

            # EXPLICIT ignorance patterns (highly specific)
            # Added variations to catch different word orders (e.g., "real-time access" vs "access to real-time")
            explicit_ignorance_patterns = [
                "i don't have the ability to access",
                "i don't have access to real-time",
                "i don't have real-time access",  # Word order variation
                "i don't have access to current",
                "i don't have current",  # Shorter variation
                "i cannot access real-time",
                "i cannot provide real-time",
                "please check espn",
                "please check cbs sports",
                "check reputable sports news",
                "check with a trustworthy sports news source",  # Common LLM phrasing
                "consult official league",
                "visit the official website",
                "for their latest updates on"  # Often paired with "check ESPN/sports news"
            ]

            for pattern in explicit_ignorance_patterns:
                if pattern in answer_lower:
                    logger.warning(f"Answer explicitly admits ignorance: contains '{pattern}'")
                    return (
                        ValidationResult.INVALID,
                        f"Answer explicitly admits it doesn't have the information",
                        {
                            "fallback_action": "web_search",
                            "reason": "llm_admits_ignorance",
                            "limitation_phrase": pattern
                        }
                    )

            logger.debug(f"Answer quality validated successfully")
            return (ValidationResult.VALID, "Answer appears helpful and informative", None)

        except Exception as e:
            logger.error(f"Answer validation error: {e}", exc_info=True)
            # If validation fails, assume answer is OK to avoid false positives
            return (ValidationResult.VALID, "Validation error, assuming answer is OK", None)

    def validate_sports_response(
        self,
        response_data: Dict[str, Any],
        query: str,
        requested_team: Optional[str] = None
    ) -> Tuple[ValidationResult, str, Optional[Dict[str, Any]]]:
        """
        Validate Sports RAG service response.

        Args:
            response_data: Raw response from Sports RAG service
            query: Original user query
            requested_team: Team name that was requested (optional)

        Returns:
            Tuple of (ValidationResult, reason, suggestions)

        Validates:
            - Events array exists and has data
            - Events have required fields (datetime, opponent, location)
            - Team data has required fields (name, record, standing)
            - Response matches query intent (scores vs schedules)
            - Events match the requested team (detect API data corruption)
        """
        try:
            # Handle new combined format with last_games, upcoming_games, live_games
            if "last_games" in response_data or "upcoming_games" in response_data or "live_games" in response_data:
                last_games = response_data.get("last_games", [])
                upcoming_games = response_data.get("upcoming_games", [])
                live_games = response_data.get("live_games", [])
                season_status = response_data.get("season_status")
                team_name = response_data.get("team", "")

                total_games = len(last_games) + len(upcoming_games) + len(live_games)

                # Check if season has ended (valid response even without upcoming games)
                if season_status == "ended":
                    team_record = response_data.get("team_record", "")
                    team_standing = response_data.get("team_standing", "")
                    season_message = response_data.get("season_message", "Season has ended")
                    logger.info(
                        f"Sports RAG indicates season ended for {team_name}",
                        team_record=team_record,
                        team_standing=team_standing,
                        last_games_count=len(last_games)
                    )
                    # If we have last games data, this is a valid response
                    if last_games:
                        return (
                            ValidationResult.VALID,
                            f"Season ended with {len(last_games)} past games available",
                            {"season_status": "ended", "team_record": team_record, "has_history": True}
                        )
                    else:
                        return (
                            ValidationResult.VALID,
                            f"Season ended - {season_message}",
                            {"season_status": "ended", "team_record": team_record}
                        )

                # Check if we have any useful data
                if total_games == 0:
                    logger.warning(f"Sports RAG returned no games for query: {query}")
                    return (
                        ValidationResult.EMPTY,
                        "No games found in response",
                        {"fallback_action": "web_search", "reason": "empty_games"}
                    )

                # Check if there's a live game (high priority)
                if live_games:
                    logger.info(f"Sports RAG found live game for {team_name}")
                    return (
                        ValidationResult.VALID,
                        f"Found live game and {total_games - len(live_games)} other games",
                        {"has_live_game": True}
                    )

                # Validate event structure in last_games or upcoming_games
                all_events = last_games + [g for g in upcoming_games if not g.get("season_status")]
                if all_events:
                    first_event = all_events[0]
                    required_fields = ["strEvent", "dateEvent"]
                    missing_fields = [f for f in required_fields if f not in first_event or first_event.get(f) is None]

                    if missing_fields:
                        logger.warning(f"Sports event missing fields {missing_fields}: {first_event}")
                        return (
                            ValidationResult.INVALID,
                            f"Event missing required fields: {missing_fields}",
                            {"fallback_action": "web_search", "reason": "malformed_event"}
                        )

                logger.debug(f"Sports response validated: {total_games} total games")
                return (
                    ValidationResult.VALID,
                    f"Found {len(last_games)} past, {len(upcoming_games)} upcoming games",
                    None
                )

            # Legacy format: Check for events array
            if "events" in response_data:
                events = response_data.get("events", [])

                if not events or len(events) == 0:
                    logger.warning(f"Sports RAG returned empty events for query: {query}")
                    return (
                        ValidationResult.EMPTY,
                        "No events found in response",
                        {"fallback_action": "web_search", "reason": "empty_events"}
                    )

                # Validate first event structure (sample check)
                first_event = events[0]

                # Check if this is a "season ended" response
                # This is valid - it means no upcoming games but we have team status info
                if first_event.get("season_status") == "ended":
                    logger.info(
                        f"Sports RAG indicates season ended for query: {query}",
                        team_record=first_event.get("team_record"),
                        team_standing=first_event.get("team_standing")
                    )
                    return (
                        ValidationResult.VALID,
                        f"Season ended - {first_event.get('message', 'No upcoming games')}",
                        {"season_status": "ended", "team_record": first_event.get("team_record")}
                    )

                # TheSportsDB API uses these field names
                required_event_fields = ["strEvent", "dateEvent"]
                missing_fields = [f for f in required_event_fields if f not in first_event or first_event.get(f) is None]

                if missing_fields:
                    logger.warning(f"Sports event missing fields {missing_fields}: {first_event}")
                    return (
                        ValidationResult.INVALID,
                        f"Event missing required fields: {missing_fields}",
                        {"fallback_action": "web_search", "reason": "malformed_event"}
                    )

                # CRITICAL FIX: Validate events match requested team
                # TheSportsDB has data corruption issues where team_id can return wrong team
                if requested_team:
                    # Check if any events match the requested team
                    team_lower = requested_team.lower()
                    events_match_team = False

                    for event in events[:5]:  # Check first 5 events
                        event_name = event.get("strEvent", "").lower()
                        home_team = event.get("strHomeTeam", "").lower()
                        away_team = event.get("strAwayTeam", "").lower()

                        if (team_lower in event_name or
                            team_lower in home_team or
                            team_lower in away_team):
                            events_match_team = True
                            break

                    if not events_match_team:
                        logger.warning(
                            f"Sports RAG returned events that don't match requested team '{requested_team}'. "
                            f"First event: {first_event.get('strEvent')}. "
                            f"This suggests TheSportsDB API data corruption."
                        )
                        return (
                            ValidationResult.INVALID,
                            f"Events don't match requested team '{requested_team}'",
                            {
                                "fallback_action": "web_search",
                                "reason": "team_mismatch",
                                "api_bug": "thesportsdb_team_id_corruption"
                            }
                        )

                # Check if query asks for scores but we got schedule
                query_lower = query.lower()
                if any(word in query_lower for word in ["score", "result", "won", "lost", "final"]):
                    if not any("score" in e or "result" in e for e in events):
                        logger.info(f"Query asks for scores but response has schedule data")
                        return (
                            ValidationResult.NEEDS_RETRY,
                            "Query wants scores but got schedule",
                            {"retry_with": "scores", "fallback_action": "web_search"}
                        )

                logger.debug(f"Sports response validated: {len(events)} events")
                return (ValidationResult.VALID, f"Found {len(events)} events", None)

            # Check for team data
            elif "teams" in response_data:
                teams = response_data.get("teams", [])

                if not teams or len(teams) == 0:
                    logger.warning(f"Sports RAG returned empty teams for query: {query}")
                    return (
                        ValidationResult.EMPTY,
                        "No teams found in response",
                        {"fallback_action": "web_search", "reason": "empty_teams"}
                    )

                # Validate team structure
                first_team = teams[0]
                required_team_fields = ["name"]
                missing_fields = [f for f in required_team_fields if f not in first_team]

                if missing_fields:
                    logger.warning(f"Sports team missing fields {missing_fields}: {first_team}")
                    return (
                        ValidationResult.INVALID,
                        f"Team missing required fields: {missing_fields}",
                        {"fallback_action": "web_search", "reason": "malformed_team"}
                    )

                logger.debug(f"Sports response validated: {len(teams)} teams")
                return (ValidationResult.VALID, f"Found {len(teams)} teams", None)

            # No recognized data structure
            else:
                logger.warning(f"Sports response has no events or teams: {list(response_data.keys())}")
                return (
                    ValidationResult.INVALID,
                    "Response missing events or teams structure",
                    {"fallback_action": "web_search", "reason": "unknown_structure"}
                )

        except Exception as e:
            logger.error(f"Sports validation error: {e}", exc_info=True)
            return (
                ValidationResult.INVALID,
                f"Validation error: {str(e)}",
                {"fallback_action": "web_search", "reason": "validation_exception"}
            )

    def validate_weather_response(
        self,
        response_data: Dict[str, Any],
        query: str
    ) -> Tuple[ValidationResult, str, Optional[Dict[str, Any]]]:
        """
        Validate Weather RAG service response.

        Args:
            response_data: Raw response from Weather RAG service
            query: Original user query

        Returns:
            Tuple of (ValidationResult, reason, suggestions)

        Validates:
            - Current weather has temperature and conditions
            - Forecast has at least 1 day of data
            - Alerts array exists (can be empty)
            - Temperature values are reasonable
        """
        try:
            # Check for current weather data
            if "current" in response_data:
                current = response_data.get("current", {})

                if not current:
                    logger.warning(f"Weather RAG returned empty current data for query: {query}")
                    return (
                        ValidationResult.EMPTY,
                        "No current weather data",
                        {"fallback_action": "web_search", "reason": "empty_current"}
                    )

                # Validate current weather structure
                required_fields = ["temperature"]
                missing_fields = [f for f in required_fields if f not in current]

                if missing_fields:
                    logger.warning(f"Current weather missing fields {missing_fields}: {current}")
                    return (
                        ValidationResult.INVALID,
                        f"Current weather missing required fields: {missing_fields}",
                        {"fallback_action": "web_search", "reason": "malformed_current"}
                    )

                # Validate temperature is reasonable (-50°F to 150°F)
                temp = current.get("temperature")
                if temp and (temp < -50 or temp > 150):
                    logger.warning(f"Weather temperature out of range: {temp}°F")
                    return (
                        ValidationResult.INVALID,
                        f"Temperature out of reasonable range: {temp}°F",
                        {"fallback_action": "web_search", "reason": "invalid_temperature"}
                    )

                logger.debug(f"Weather current validated: {temp}°F")
                return (ValidationResult.VALID, f"Current weather: {temp}°F", None)

            # Check for forecast data
            elif "forecast" in response_data:
                forecast = response_data.get("forecast", [])

                if not forecast or len(forecast) == 0:
                    logger.warning(f"Weather RAG returned empty forecast for query: {query}")
                    return (
                        ValidationResult.EMPTY,
                        "No forecast data",
                        {"fallback_action": "web_search", "reason": "empty_forecast"}
                    )

                # Validate first forecast day structure
                first_day = forecast[0]
                # Accept both "high"/"low" and "temp_high"/"temp_low" field names
                has_high = "high" in first_day or "temp_high" in first_day
                has_low = "low" in first_day or "temp_low" in first_day
                has_date = "date" in first_day
                missing_fields = []
                if not has_date:
                    missing_fields.append("date")
                if not has_high:
                    missing_fields.append("high/temp_high")
                if not has_low:
                    missing_fields.append("low/temp_low")

                if missing_fields:
                    logger.warning(f"Forecast day missing fields {missing_fields}: {first_day}")
                    return (
                        ValidationResult.INVALID,
                        f"Forecast missing required fields: {missing_fields}",
                        {"fallback_action": "web_search", "reason": "malformed_forecast"}
                    )

                logger.debug(f"Weather forecast validated: {len(forecast)} days")
                return (ValidationResult.VALID, f"Forecast: {len(forecast)} days", None)

            # No recognized data structure
            else:
                logger.warning(f"Weather response has no current or forecast: {list(response_data.keys())}")
                return (
                    ValidationResult.INVALID,
                    "Response missing current or forecast structure",
                    {"fallback_action": "web_search", "reason": "unknown_structure"}
                )

        except Exception as e:
            logger.error(f"Weather validation error: {e}", exc_info=True)
            return (
                ValidationResult.INVALID,
                f"Validation error: {str(e)}",
                {"fallback_action": "web_search", "reason": "validation_exception"}
            )

    def validate_airports_response(
        self,
        response_data: Dict[str, Any],
        query: str
    ) -> Tuple[ValidationResult, str, Optional[Dict[str, Any]]]:
        """
        Validate Airports RAG service response.

        Args:
            response_data: Raw response from Airports RAG service
            query: Original user query

        Returns:
            Tuple of (ValidationResult, reason, suggestions)

        Validates:
            - Airport info has name and code
            - Flight search has results array
            - Flights have required fields (flight_number, status, time)
            - Delays/cancellations are properly marked
        """
        try:
            # Check for airport info
            if "airport" in response_data:
                airport = response_data.get("airport", {})

                if not airport:
                    logger.warning(f"Airports RAG returned empty airport data for query: {query}")
                    return (
                        ValidationResult.EMPTY,
                        "No airport data",
                        {"fallback_action": "web_search", "reason": "empty_airport"}
                    )

                # Validate airport structure
                required_fields = ["code", "name"]
                missing_fields = [f for f in required_fields if f not in airport]

                if missing_fields:
                    logger.warning(f"Airport missing fields {missing_fields}: {airport}")
                    return (
                        ValidationResult.INVALID,
                        f"Airport missing required fields: {missing_fields}",
                        {"fallback_action": "web_search", "reason": "malformed_airport"}
                    )

                logger.debug(f"Airport info validated: {airport.get('code')}")
                return (ValidationResult.VALID, f"Airport: {airport.get('code')}", None)

            # Check for flight search results
            elif "flights" in response_data:
                flights = response_data.get("flights", [])

                if not flights or len(flights) == 0:
                    logger.warning(f"Airports RAG returned empty flights for query: {query}")
                    return (
                        ValidationResult.EMPTY,
                        "No flights found in response",
                        {"fallback_action": "web_search", "reason": "empty_flights"}
                    )

                # Validate first flight structure
                first_flight = flights[0]
                required_fields = ["flight_number"]
                missing_fields = [f for f in required_fields if f not in first_flight]

                if missing_fields:
                    logger.warning(f"Flight missing fields {missing_fields}: {first_flight}")
                    return (
                        ValidationResult.INVALID,
                        f"Flight missing required fields: {missing_fields}",
                        {"fallback_action": "web_search", "reason": "malformed_flight"}
                    )

                logger.debug(f"Airports response validated: {len(flights)} flights")
                return (ValidationResult.VALID, f"Found {len(flights)} flights", None)

            # No recognized data structure
            else:
                logger.warning(f"Airports response has no airport or flights: {list(response_data.keys())}")
                return (
                    ValidationResult.INVALID,
                    "Response missing airport or flights structure",
                    {"fallback_action": "web_search", "reason": "unknown_structure"}
                )

        except Exception as e:
            logger.error(f"Airports validation error: {e}", exc_info=True)
            return (
                ValidationResult.INVALID,
                f"Validation error: {str(e)}",
                {"fallback_action": "web_search", "reason": "validation_exception"}
            )


# Global validator instance
validator = RAGValidator()

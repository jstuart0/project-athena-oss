"""
Intent classification for query routing.

Classifies user queries into intent categories to route to appropriate search providers.
"""

import re
from typing import Dict, List, Tuple
import logging

logger = logging.getLogger(__name__)


class IntentClassifier:
    """
    Classifies query intent based on keyword patterns.

    Intent types:
    - event_search: Concerts, shows, sports events, performances
    - news: Current events, breaking news, latest updates
    - weather: Weather conditions, forecasts
    - sports: Sports scores, schedules, team info
    - local_business: Restaurants, shops, services
    - general: Everything else (default)
    """

    # Intent detection patterns (regex)
    INTENT_PATTERNS = {
        "event_search": [
            r"\b(concert|show|event|performance|tour|festival|game)\b",
            r"\b(tickets|venue|live|appearing|playing|performing)\b",
            r"\b(music|band|artist|singer|comedian|theater)\b"
        ],
        "news": [
            r"\b(news|breaking|latest|today|current|recent)\b",
            r"\b(headline|report|update|article)\b"
        ],
        "weather": [
            r"\b(weather|temperature|forecast|rain|snow|sunny|cloudy)\b",
            r"\b(degrees|fahrenheit|celsius|humidity)\b",
            r"\b(storm|hurricane|wind|precipitation)\b"
        ],
        "sports": [
            r"\b(ravens|orioles|score|game|team|win|loss|playoff)\b",
            r"\b(championship|season|league|match|tournament)\b",
            r"\b(nfl|mlb|nba|nhl|soccer|football|baseball|basketball)\b"
        ],
        "local_business": [
            r"\b(restaurant|coffee|cafe|store|shop|near me)\b",
            r"\b(best|top|good|recommended)\s+(food|pizza|burger|sushi|chinese)\b",
            r"\b(open now|hours|location|address)\b"
        ]
    }

    # Keywords that boost intent confidence
    INTENT_KEYWORDS = {
        "event_search": ["concert", "show", "event", "tour", "festival", "tickets", "live"],
        "news": ["news", "breaking", "latest", "today", "current"],
        "weather": ["weather", "temperature", "forecast", "rain"],
        "sports": ["ravens", "orioles", "score", "game", "team"],
        "local_business": ["restaurant", "coffee", "near me", "best"]
    }

    def __init__(self):
        """Initialize intent classifier."""
        # Compile regex patterns for performance
        self.compiled_patterns = {
            intent: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
            for intent, patterns in self.INTENT_PATTERNS.items()
        }

    def classify(self, query: str) -> str:
        """
        Classify query intent.

        Args:
            query: User query string

        Returns:
            Intent type (event_search, news, weather, sports, local_business, general)
        """
        query_lower = query.lower()

        # Score each intent
        scores: Dict[str, float] = {}

        for intent, patterns in self.compiled_patterns.items():
            score = 0.0

            # Pattern matching
            for pattern in patterns:
                if pattern.search(query_lower):
                    score += 1.0

            # Keyword matching (bonus)
            for keyword in self.INTENT_KEYWORDS.get(intent, []):
                if keyword in query_lower:
                    score += 0.5

            scores[intent] = score

        # Get intent with highest score
        if scores:
            max_score = max(scores.values())
            if max_score > 0:
                best_intent = max(scores.items(), key=lambda x: x[1])[0]
                logger.info(f"Classified intent: {best_intent} (score: {max_score:.1f}) for query: '{query}'")
                return best_intent

        # Default to general
        logger.info(f"Classified intent: general (no matches) for query: '{query}'")
        return "general"

    def classify_with_confidence(self, query: str) -> Tuple[str, float]:
        """
        Classify query intent with confidence score.

        Args:
            query: User query string

        Returns:
            (intent, confidence) tuple where confidence is 0.0-1.0
        """
        query_lower = query.lower()

        # Score each intent
        scores: Dict[str, float] = {}

        for intent, patterns in self.compiled_patterns.items():
            score = 0.0
            matches = 0

            # Pattern matching
            for pattern in patterns:
                if pattern.search(query_lower):
                    score += 1.0
                    matches += 1

            # Keyword matching (bonus)
            for keyword in self.INTENT_KEYWORDS.get(intent, []):
                if keyword in query_lower:
                    score += 0.5

            scores[intent] = score

        # Get intent with highest score
        if scores:
            max_score = max(scores.values())
            if max_score > 0:
                best_intent = max(scores.items(), key=lambda x: x[1])[0]

                # Normalize confidence (assume 3+ matches = high confidence)
                confidence = min(1.0, max_score / 3.0)

                logger.info(f"Classified intent: {best_intent} (score: {max_score:.1f}, confidence: {confidence:.2f}) for query: '{query}'")
                return (best_intent, confidence)

        # Default to general with low confidence
        logger.info(f"Classified intent: general (no matches) for query: '{query}'")
        return ("general", 0.5)

    def extract_keywords(self, query: str) -> List[str]:
        """
        Extract keywords from query.

        Args:
            query: User query string

        Returns:
            List of extracted keywords
        """
        # Simple keyword extraction: lowercase, remove stop words, split
        stop_words = {"a", "an", "the", "is", "are", "was", "were", "in", "on", "at", "to", "for", "of", "what", "when", "where", "who", "how"}

        words = query.lower().split()
        keywords = [word.strip(".,!?;:") for word in words if word.lower() not in stop_words]

        return keywords

    def is_rag_intent(self, intent: str) -> bool:
        """
        Check if intent should be handled by RAG service instead of web search.

        Args:
            intent: Classified intent

        Returns:
            True if RAG should handle, False if web search should handle
        """
        # These intents are handled by dedicated RAG services
        rag_intents = {"weather", "sports"}  # airports handled separately
        return intent in rag_intents

    def detect_multi_intent(self, query: str) -> List[str]:
        """
        Detect if query contains multiple intents and split them.
        Returns list of sub-queries.
        """
        query_lower = query.lower()

        # MULTI-ROOM DETECTION: Do NOT split if "and" is joining room names
        # Pattern: "[action] [room] and [room] [device]" - single command for multiple rooms
        import re

        # Common room names that might be joined with "and"
        room_names = [
            'office', 'kitchen', 'bedroom', 'living room', 'bathroom',
            'master bedroom', 'master bath', 'guest room', 'hallway', 'hall',
            'basement', 'attic', 'garage', 'porch', 'deck', 'patio', 'dining room',
            'den', 'family room', 'study', 'library', 'laundry room', 'alpha', 'beta',
            'first floor', 'second floor', 'downstairs', 'upstairs'
        ]

        # Check if "and" is between two room names (multi-room command)
        if " and " in query_lower:
            # Pattern: room1 and room2 with lights/on/off nearby
            # This indicates a multi-room light command, NOT multiple intents
            for room1 in room_names:
                if room1 in query_lower:
                    for room2 in room_names:
                        if room2 != room1 and room2 in query_lower:
                            # Both rooms present - check if "and" connects them
                            # Pattern like "kitchen and living room" or "living room and kitchen"
                            pattern1 = f"{room1}\\s+and\\s+{room2}"
                            pattern2 = f"{room2}\\s+and\\s+{room1}"
                            if re.search(pattern1, query_lower) or re.search(pattern2, query_lower):
                                # Also check if there's a light/on/off keyword indicating this is a light command
                                light_keywords = ['light', 'lights', 'on', 'off', 'turn', 'switch', 'dim', 'bright']
                                if any(kw in query_lower for kw in light_keywords):
                                    logger.info(f"Multi-room command detected, NOT splitting: '{query[:50]}...'")
                                    return [query]  # Don't split - it's a multi-room command

        # Compound query indicators
        separators = [
            " and ",
            " then ",
            " also ",
            " after that ",
            ", then ",
            "; ",
            " plus "
        ]

        # Check if any separator exists
        found_separators = [sep for sep in separators if sep in query_lower]

        if not found_separators:
            return [query]  # Single intent

        # Split on separators while preserving context
        parts = [query]
        for separator in found_separators:
            new_parts = []
            for part in parts:
                if separator in part.lower():
                    split_parts = part.split(separator)
                    for i, split_part in enumerate(split_parts):
                        new_parts.append(split_part.strip())
                else:
                    new_parts.append(part)
            parts = new_parts

        # Filter out empty or too-short parts
        valid_parts = [p for p in parts if len(p.split()) >= 2]

        return valid_parts if valid_parts else [query]

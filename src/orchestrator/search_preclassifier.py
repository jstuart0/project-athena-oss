"""
Search Pre-Classification Module

Uses embedding similarity to pre-classify obvious search queries without
requiring LLM inference. Saves ~1.3s on high-confidence matches.

This is Option D from the optimization proposal:
- Pre-compute embeddings for canonical query templates
- Compare new query embedding to known templates
- Fast (~50ms) and handles variations better than regex

When confidence is below threshold, falls back to LLM classification.
"""

import numpy as np
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass
import structlog

logger = structlog.get_logger()


@dataclass
class IntentMatch:
    """Result of pre-classification."""
    intent: str
    confidence: float
    matched_template: str
    skip_llm: bool  # True if confidence is high enough to skip LLM


# Canonical query templates for each intent
# These are representative queries that define each category
INTENT_TEMPLATES: Dict[str, List[str]] = {
    "dining": [
        "find restaurants near me",
        "where should I eat dinner",
        "good Italian place nearby",
        "recommend a restaurant",
        "search for food places",
        "looking for somewhere to eat",
        "best sushi restaurant",
        "find a cafe close by",
        "where can I get lunch",
        "search restaurants in Baltimore",
    ],
    "events": [
        "find events this weekend",
        "concerts near me",
        "what's happening tonight",
        "search for shows",
        "events in the city",
        "find tickets for",
        "local events this week",
        "festivals coming up",
        "theater shows nearby",
        "search concerts in Baltimore",
    ],
    "weather": [
        "what's the weather today",
        "weather forecast",
        "will it rain tomorrow",
        "temperature outside",
        "how's the weather",
        "weather in Baltimore",
        "forecast for this weekend",
        "is it going to snow",
        "current weather conditions",
        "should I bring an umbrella",
    ],
    "sports": [
        "when is the next game",
        "sports scores today",
        "Ravens game schedule",
        "who won the game",
        "basketball scores",
        "NFL standings",
        "next football game",
        "how did the Orioles do",
        "sports schedule",
        "game tonight",
    ],
    "flights": [
        "flights to Miami",
        "search for flights",
        "flight status",
        "when does my flight arrive",
        "flights from Baltimore",
        "book a flight to",
        "airline schedules",
        "flight times to New York",
        "airplane tickets",
        "departures from BWI",
    ],
    "news": [
        "what's in the news",
        "latest headlines",
        "news today",
        "current events",
        "breaking news",
        "top stories",
        "news about technology",
        "world news",
        "local news",
        "news headlines",
    ],
    "stocks": [
        "stock price of Apple",
        "how is the market",
        "AAPL stock",
        "market update",
        "stock quote for Tesla",
        "how are my stocks",
        "NASDAQ today",
        "S&P 500",
        "stock performance",
        "check stock price",
    ],
    "streaming": [
        "what to watch on Netflix",
        "is this movie on streaming",
        "find something to watch",
        "where can I stream",
        "movies on Hulu",
        "TV shows to watch",
        "streaming availability",
        "what's new on Disney Plus",
        "search for movies",
        "recommended shows",
    ],
    "control": [
        "turn on the lights",
        "turn off the bedroom light",
        "set temperature to 72",
        "lock the front door",
        "open the garage",
        "dim the lights",
        "turn on the fan",
        "close the blinds",
        "arm the security system",
        "set a timer",
    ],
    "status": [
        "what lights are on",
        "is the door locked",
        "current thermostat setting",
        "status of the garage",
        "are any windows open",
        "which lights are off",
        "check the door status",
        "is the alarm set",
        "what's the temperature inside",
        "are all doors locked",
    ],
}


class SearchPreClassifier:
    """
    Pre-classifies queries using embedding similarity.

    Uses sentence-transformers for fast, accurate semantic matching.
    Falls back to keyword matching if embeddings unavailable.
    """

    def __init__(self, confidence_threshold: float = 0.85):
        """
        Initialize the pre-classifier.

        Args:
            confidence_threshold: Minimum confidence to skip LLM (default 0.85)
        """
        self.confidence_threshold = confidence_threshold
        self.model = None
        self.template_embeddings: Dict[str, np.ndarray] = {}
        self._initialized = False

    async def initialize(self) -> bool:
        """
        Lazy-load the embedding model and pre-compute template embeddings.

        Returns:
            True if initialization successful, False otherwise
        """
        if self._initialized:
            return self.model is not None

        try:
            from sentence_transformers import SentenceTransformer

            # Use same model as intent_discovery for consistency
            self.model = SentenceTransformer("all-MiniLM-L6-v2")

            # Pre-compute embeddings for all templates
            for intent, templates in INTENT_TEMPLATES.items():
                # Compute mean embedding for all templates of this intent
                embeddings = self.model.encode(templates)
                self.template_embeddings[intent] = np.mean(embeddings, axis=0)

            self._initialized = True
            logger.info(
                "search_preclassifier_initialized",
                model="all-MiniLM-L6-v2",
                intents=list(INTENT_TEMPLATES.keys())
            )
            return True

        except ImportError:
            logger.warning(
                "sentence_transformers_not_installed",
                message="Search pre-classification will use keyword fallback"
            )
            self._initialized = True
            return False

        except Exception as e:
            logger.error("search_preclassifier_init_failed", error=str(e))
            self._initialized = True
            return False

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two vectors."""
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    def _keyword_classify(self, query: str) -> Optional[IntentMatch]:
        """
        Fallback keyword-based classification.

        Used when embedding model is unavailable.
        """
        query_lower = query.lower()

        # Keyword patterns for each intent
        keyword_patterns = {
            "dining": ["restaurant", "eat", "food", "dinner", "lunch", "breakfast", "cafe", "cuisine"],
            "events": ["event", "concert", "show", "ticket", "festival", "happening"],
            "weather": ["weather", "temperature", "rain", "forecast", "sunny", "cold", "hot"],
            "sports": ["game", "score", "team", "nfl", "nba", "mlb", "match", "player"],
            "flights": ["flight", "airport", "airline", "plane", "fly", "departure", "arrival"],
            "news": ["news", "headline", "breaking", "story", "current events"],
            "stocks": ["stock", "market", "nasdaq", "price", "share", "invest"],
            "streaming": ["watch", "netflix", "stream", "movie", "show", "hulu", "disney"],
            "control": ["turn", "set", "lock", "unlock", "open", "close", "dim", "bright"],
            "status": ["status", "what", "are", "is the", "check", "which"],
        }

        best_intent = None
        best_score = 0

        for intent, keywords in keyword_patterns.items():
            matches = sum(1 for kw in keywords if kw in query_lower)
            score = matches / len(keywords)
            if score > best_score:
                best_score = score
                best_intent = intent

        if best_intent and best_score > 0.2:
            # Keyword matching is less confident, so apply a penalty
            confidence = min(best_score * 0.7, 0.7)  # Cap at 0.7 for keyword matches
            return IntentMatch(
                intent=best_intent,
                confidence=confidence,
                matched_template="keyword_match",
                skip_llm=False  # Always require LLM verification for keyword matches
            )

        return None

    async def classify(self, query: str, feature_config: Dict[str, Any] = None) -> Optional[IntentMatch]:
        """
        Pre-classify a query using embedding similarity.

        Args:
            query: The user query to classify
            feature_config: Feature flag configuration with confidence_threshold

        Returns:
            IntentMatch if classification successful, None if should use LLM
        """
        if not self._initialized:
            await self.initialize()

        # Get confidence threshold from config or use default
        threshold = self.confidence_threshold
        if feature_config and "confidence_threshold" in feature_config:
            threshold = feature_config["confidence_threshold"]

        # If model not available, try keyword fallback
        if self.model is None:
            return self._keyword_classify(query)

        try:
            # Compute embedding for the query
            query_embedding = self.model.encode(query)

            best_intent = None
            best_similarity = 0.0
            best_template = ""

            # Compare against all intent template embeddings
            for intent, template_embedding in self.template_embeddings.items():
                similarity = self._cosine_similarity(query_embedding, template_embedding)
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_intent = intent
                    # Find closest individual template for debugging
                    individual_embeddings = self.model.encode(INTENT_TEMPLATES[intent])
                    individual_sims = [
                        self._cosine_similarity(query_embedding, emb)
                        for emb in individual_embeddings
                    ]
                    best_idx = np.argmax(individual_sims)
                    best_template = INTENT_TEMPLATES[intent][best_idx]

            if best_intent:
                skip_llm = best_similarity >= threshold
                result = IntentMatch(
                    intent=best_intent,
                    confidence=best_similarity,
                    matched_template=best_template,
                    skip_llm=skip_llm
                )

                logger.info(
                    "search_preclassified",
                    query=query[:50],
                    intent=best_intent,
                    confidence=round(best_similarity, 3),
                    skip_llm=skip_llm,
                    matched_template=best_template[:30]
                )

                return result

            return None

        except Exception as e:
            logger.warning("search_preclassify_error", error=str(e), query=query[:50])
            return self._keyword_classify(query)

    def update_threshold(self, threshold: float):
        """Update the confidence threshold for skipping LLM."""
        self.confidence_threshold = threshold
        logger.info("preclassifier_threshold_updated", threshold=threshold)


# Global instance for reuse
_preclassifier: Optional[SearchPreClassifier] = None


async def get_preclassifier() -> SearchPreClassifier:
    """Get or create the global pre-classifier instance."""
    global _preclassifier
    if _preclassifier is None:
        _preclassifier = SearchPreClassifier()
        await _preclassifier.initialize()
    return _preclassifier


async def preclassify_query(
    query: str,
    feature_enabled: bool = True,
    feature_config: Dict[str, Any] = None
) -> Optional[IntentMatch]:
    """
    Convenience function to pre-classify a query.

    Args:
        query: The user query
        feature_enabled: Whether pre-classification feature is enabled
        feature_config: Feature configuration from admin

    Returns:
        IntentMatch if classification successful and confident, None otherwise
    """
    if not feature_enabled:
        return None

    preclassifier = await get_preclassifier()
    return await preclassifier.classify(query, feature_config)

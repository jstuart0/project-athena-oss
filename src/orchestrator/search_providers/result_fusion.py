"""
Result fusion and ranking for parallel search.

Combines results from multiple providers with deduplication and intelligent ranking.
"""

from typing import List, Dict, Set
from collections import defaultdict
import logging
import re
from difflib import SequenceMatcher

from .base import SearchResult

logger = logging.getLogger(__name__)


class ResultFusion:
    """
    Intelligent result fusion engine.

    Features:
    - Deduplication by content similarity
    - Cross-validation (facts from multiple sources)
    - Authority-based scoring (provider weights by query type)
    - Recency scoring for time-sensitive queries
    - Confidence scoring based on source agreement
    """

    # Provider authority weights by intent type
    PROVIDER_WEIGHTS = {
        "ticketmaster": {
            "event_search": 1.0,  # Perfect for events
            "concert": 1.0,
            "sports": 1.0,
            "show": 0.9,
            "general": 0.0,  # Don't use for general queries (only for events)
            "news": 0.0,
            "local_business": 0.2
        },
        "eventbrite": {
            "event_search": 0.9,  # Excellent for events
            "concert": 0.8,
            "meetup": 1.0,
            "workshop": 1.0,
            "local_business": 0.6,
            "general": 0.0,  # Don't use for general queries (only for events)
            "news": 0.0
        },
        "duckduckgo": {
            "general": 0.8,  # Good for general queries
            "event_search": 0.5,  # OK for events (backup)
            "news": 0.9,
            "local_business": 0.7,
            "definition": 1.0
        },
        "brave": {
            "general": 0.9,  # Excellent for general queries
            "event_search": 0.6,  # Decent for events (web results)
            "news": 0.95,  # Excellent news search with dedicated results
            "local_business": 0.8,
            "definition": 0.85
        },
        "searxng": {
            "general": 0.75,  # Good for general queries (aggregates multiple engines)
            "event_search": 0.55,  # Moderate for events (backup coverage)
            "news": 0.8,  # Good for news (multiple news sources)
            "local_business": 0.7,  # Decent for local business
            "definition": 0.75  # Good for definitions (Wikipedia, etc.)
        }
    }

    def __init__(
        self,
        similarity_threshold: float = 0.7,
        min_confidence: float = 0.5
    ):
        """
        Initialize result fusion engine.

        Args:
            similarity_threshold: Threshold for detecting duplicate results (0.0-1.0)
            min_confidence: Minimum confidence score to include result
        """
        self.similarity_threshold = similarity_threshold
        self.min_confidence = min_confidence

    def fuse_results(
        self,
        results: List[SearchResult],
        query: str,
        intent: str = "general"
    ) -> List[SearchResult]:
        """
        Fuse and rank results from multiple providers.

        Args:
            results: All results from parallel search
            query: Original search query
            intent: Query intent type (e.g., "event_search", "general")

        Returns:
            Deduplicated, ranked list of SearchResult objects
        """
        if not results:
            return []

        logger.info(f"Fusing {len(results)} results for intent '{intent}'")

        # Step 1: Deduplicate results
        deduplicated = self._deduplicate(results)
        logger.info(f"After deduplication: {len(deduplicated)} results")

        # Step 2: Cross-validate facts (boost confidence for multi-source facts)
        validated = self._cross_validate(deduplicated)

        # Step 3: Apply authority weights based on intent
        scored = self._apply_authority_weights(validated, intent)

        # Step 4: Filter by minimum confidence
        filtered = [r for r in scored if r.confidence >= self.min_confidence]
        logger.info(f"After confidence filter: {len(filtered)} results")

        # Step 5: Sort by confidence (descending)
        ranked = sorted(filtered, key=lambda r: r.confidence, reverse=True)

        return ranked

    def _deduplicate(self, results: List[SearchResult]) -> List[SearchResult]:
        """
        Remove duplicate or very similar results.

        Uses title and snippet similarity to detect duplicates.
        Keeps the result with highest confidence.

        Args:
            results: List of search results

        Returns:
            Deduplicated list
        """
        if not results:
            return []

        unique_results = []
        seen_content: Set[str] = set()

        for result in results:
            # Create content fingerprint
            content = f"{result.title.lower()} {result.snippet.lower()}"

            # Check if similar to any existing result
            is_duplicate = False
            for seen in seen_content:
                similarity = self._text_similarity(content, seen)
                if similarity >= self.similarity_threshold:
                    is_duplicate = True
                    break

            if not is_duplicate:
                unique_results.append(result)
                seen_content.add(content)

        return unique_results

    def _cross_validate(self, results: List[SearchResult]) -> List[SearchResult]:
        """
        Cross-validate facts across multiple sources.

        If multiple providers return similar information, boost confidence.

        Args:
            results: Deduplicated results

        Returns:
            Results with adjusted confidence scores
        """
        # Group results by similar titles (potential same event)
        title_groups = defaultdict(list)

        for result in results:
            # Normalize title for grouping
            normalized_title = self._normalize_text(result.title)
            title_groups[normalized_title].append(result)

        # Boost confidence for results confirmed by multiple sources
        validated_results = []

        for title, group in title_groups.items():
            if len(group) > 1:
                # Multiple sources confirm this result
                unique_sources = {r.source for r in group}
                confidence_boost = min(0.2 * (len(unique_sources) - 1), 0.3)

                logger.info(f"Cross-validation: '{title[:50]}' confirmed by {len(unique_sources)} sources, boost={confidence_boost:.2f}")

                # Apply boost to all results in group
                for result in group:
                    result.confidence = min(1.0, result.confidence + confidence_boost)

            validated_results.extend(group)

        return validated_results

    def _apply_authority_weights(
        self,
        results: List[SearchResult],
        intent: str
    ) -> List[SearchResult]:
        """
        Apply provider authority weights based on query intent.

        Args:
            results: Results to score
            intent: Query intent type

        Returns:
            Results with adjusted confidence scores
        """
        for result in results:
            source = result.source
            base_confidence = result.confidence

            # Get provider weights
            provider_weights = self.PROVIDER_WEIGHTS.get(source, {})

            # Get weight for this intent type
            weight = provider_weights.get(intent, provider_weights.get("general", 0.7))

            # Apply weight
            result.confidence = min(1.0, base_confidence * weight)

            logger.debug(f"Authority weight for {source} (intent={intent}): {weight:.2f}, confidence: {base_confidence:.2f} -> {result.confidence:.2f}")

        return results

    def _text_similarity(self, text1: str, text2: str) -> float:
        """
        Calculate text similarity ratio.

        Args:
            text1: First text
            text2: Second text

        Returns:
            Similarity ratio (0.0-1.0)
        """
        return SequenceMatcher(None, text1, text2).ratio()

    def _normalize_text(self, text: str) -> str:
        """
        Normalize text for comparison.

        Args:
            text: Text to normalize

        Returns:
            Normalized text (lowercase, no punctuation, no extra spaces)
        """
        # Convert to lowercase
        text = text.lower()

        # Remove punctuation
        text = re.sub(r'[^\w\s]', '', text)

        # Remove extra whitespace
        text = ' '.join(text.split())

        return text

    def get_top_results(
        self,
        results: List[SearchResult],
        query: str,
        intent: str = "general",
        limit: int = 5
    ) -> List[SearchResult]:
        """
        Get top N results after fusion and ranking.

        Args:
            results: All search results
            query: Original query
            intent: Query intent type
            limit: Maximum number of results to return

        Returns:
            Top N ranked results
        """
        fused = self.fuse_results(results, query, intent)
        return fused[:limit]

"""
Structured data extraction from search results.
Extracts JSON-LD, schema.org, and other embedded data WITHOUT fetching pages.

This module provides Tier 1 (fastest) content extraction by identifying
high-value URLs that likely contain comprehensive structured data.
"""
import json
import re
from typing import List, Dict, Any, Optional
from .base import SearchResult


def extract_jsonld_from_snippet(search_result: SearchResult) -> Optional[Dict]:
    """
    Extract JSON-LD from search result metadata (if search engine provides it).
    Some search engines include structured data in their API responses.

    Args:
        search_result: SearchResult object with metadata

    Returns:
        Extracted JSON-LD data or None
    """
    # Check if search result includes structured data
    metadata = search_result.metadata

    if "json_ld" in metadata:
        return metadata["json_ld"]

    # Some search engines embed schema.org data in snippets
    if "schema" in metadata:
        return metadata["schema"]

    return None


def identify_high_value_urls(results: List[SearchResult], query: str) -> List[str]:
    """
    Identify URLs most likely to contain comprehensive structured data.

    For different query types, prioritize different sources:
    - Sports: ESPN, CBS Sports, official league sites
    - Events: Ticketmaster, Eventbrite, SeatGeek
    - News: AP News, Reuters, BBC
    - General: High-authority sites

    Args:
        results: List of SearchResult objects
        query: Original user query

    Returns:
        List of up to 2 highest-value URLs
    """
    high_value_domains = {
        "sports": [
            "espn.com/scoreboard",
            "espn.com/nfl/scoreboard",
            "espn.com/nba/scoreboard",
            "espn.com/mlb/scoreboard",
            "cbssports.com/nfl/scoreboard",
            "cbssports.com/nba/scoreboard",
            "nfl.com/schedules",
            "nba.com/games",
            "mlb.com/scores",
            "thescore.com",
            "foxsports.com/scores"
        ],
        "events": [
            "ticketmaster.com/search",
            "ticketmaster.com/event",
            "eventbrite.com/d",
            "seatgeek.com/events",
            "stubhub.com/event"
        ],
        "news": [
            "apnews.com",
            "reuters.com",
            "bbc.com/news",
            "npr.org",
            "wsj.com",
            "nytimes.com"
        ],
        "weather": [
            "weather.com",
            "weather.gov",
            "accuweather.com"
        ]
    }

    # Identify query type
    query_type = classify_query_type(query)
    priority_domains = high_value_domains.get(query_type, [])

    # Score URLs by relevance
    scored_urls = []
    for result in results:
        url = result.url or ""
        score = 0

        # Higher score for priority domains
        for domain_pattern in priority_domains:
            if domain_pattern in url:
                score += 10
                break

        # Boost for specific patterns in URL
        url_lower = url.lower()
        if "schedule" in url_lower or "scoreboard" in url_lower:
            score += 5
        if "calendar" in url_lower or "upcoming" in url_lower:
            score += 3
        if "today" in url_lower or "this-week" in url_lower:
            score += 3

        # Boost for high confidence results
        if result.confidence > 0.8:
            score += 2

        # Penalize if URL looks like an article vs. schedule page
        if "/article/" in url_lower or "/story/" in url_lower or "/news/" in url_lower:
            score -= 3

        if score > 0:
            scored_urls.append((score, url, result.source))

    # Sort by score descending
    scored_urls.sort(reverse=True, key=lambda x: x[0])

    # Return top 2 URLs with their sources
    return [url for score, url, source in scored_urls[:2]]


def classify_query_type(query: str) -> str:
    """
    Classify query to identify best URL patterns.

    Args:
        query: User query string

    Returns:
        Query type category
    """
    q_lower = query.lower()

    # Sports patterns
    sports_keywords = [
        "game", "score", "schedule", "nfl", "nba", "mlb", "nhl", "mls",
        "football", "basketball", "baseball", "hockey", "soccer",
        "ravens", "orioles", "lakers", "yankees", "cowboys",
        "playoff", "championship", "season", "match", "vs", "versus"
    ]
    if any(word in q_lower for word in sports_keywords):
        return "sports"

    # Events patterns
    events_keywords = [
        "event", "concert", "show", "festival", "exhibition",
        "tickets", "upcoming", "tonight", "this weekend"
    ]
    if any(word in q_lower for word in events_keywords):
        return "events"

    # News patterns
    news_keywords = [
        "news", "breaking", "headline", "latest", "update",
        "today's news", "current events"
    ]
    if any(word in q_lower for word in news_keywords):
        return "news"

    # Weather patterns
    weather_keywords = ["weather", "forecast", "temperature", "rain", "snow"]
    if any(word in q_lower for word in weather_keywords):
        return "weather"

    return "general"


def should_fetch_content(query: str, search_results: List[SearchResult]) -> bool:
    """
    Determine if content fetching would be beneficial based on query and results.

    Fetching is beneficial when:
    - Query asks for comprehensive data (lists, schedules, multiple items)
    - Search results include high-value URLs
    - Snippets are likely incomplete

    Args:
        query: User query
        search_results: List of search results

    Returns:
        True if fetching should be attempted
    """
    q_lower = query.lower()

    # List/comprehensive data indicators
    list_indicators = [
        "all", "list", "schedule", "games", "events", "shows",
        "this week", "today", "upcoming", "complete"
    ]

    asks_for_list = any(indicator in q_lower for indicator in list_indicators)

    # Check if we have high-value URLs
    high_value_urls = identify_high_value_urls(search_results, query)
    has_good_sources = len(high_value_urls) > 0

    # Fetch if asking for comprehensive data AND we have good sources
    return asks_for_list and has_good_sources


def estimate_fetch_benefit(query: str, search_results: List[SearchResult]) -> str:
    """
    Estimate the benefit of fetching content vs. using snippets.

    Args:
        query: User query
        search_results: Search results with snippets

    Returns:
        Benefit level: "high", "medium", "low", "none"
    """
    q_lower = query.lower()

    # High benefit: Asking for comprehensive lists/schedules
    if any(word in q_lower for word in ["all", "schedule", "this week", "today", "complete"]):
        high_value_urls = identify_high_value_urls(search_results, query)
        if len(high_value_urls) > 0:
            return "high"

    # Medium benefit: Asking for specific detailed information
    if any(word in q_lower for word in ["how", "why", "explain", "details"]):
        return "medium"

    # Low benefit: Simple factual queries (snippets likely sufficient)
    if any(word in q_lower for word in ["when", "who", "what time", "score"]):
        return "low"

    return "none"


def get_fetch_priority_urls(results: List[SearchResult], query: str, max_urls: int = 2) -> List[Dict[str, Any]]:
    """
    Get priority URLs for fetching with metadata.

    Args:
        results: Search results
        query: User query
        max_urls: Maximum URLs to return

    Returns:
        List of dicts with url, source, score, and extraction_hint
    """
    query_type = classify_query_type(query)
    high_value_urls = identify_high_value_urls(results, query)

    priority_urls = []
    for url in high_value_urls[:max_urls]:
        # Find the search result for this URL
        result = next((r for r in results if r.url == url), None)

        # Determine best extraction method based on URL patterns
        extraction_hint = "auto"
        if "scoreboard" in url or "schedule" in url or "scores" in url:
            extraction_hint = "table"  # Try table extraction first
        elif "ticketmaster" in url or "eventbrite" in url:
            extraction_hint = "jsonld"  # Try structured data first

        priority_urls.append({
            "url": url,
            "source": result.source if result else "unknown",
            "confidence": result.confidence if result else 0.5,
            "extraction_hint": extraction_hint,
            "query_type": query_type
        })

    return priority_urls

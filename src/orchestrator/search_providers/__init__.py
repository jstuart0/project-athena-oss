"""
Search providers package for parallel web search.

Provides unified interface for multiple search APIs:
- DuckDuckGo Instant Answer
- Ticketmaster Discovery API
- Eventbrite API
- (Future: Brave Search, SerpAPI, etc.)
"""

from .base import SearchResult, SearchProvider

__all__ = ["SearchResult", "SearchProvider"]

"""
Base classes and data models for search providers.

Provides common interface for all search providers (DuckDuckGo, Ticketmaster, Eventbrite, etc.)
"""

from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field
from abc import ABC, abstractmethod
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class SearchResult(BaseModel):
    """Normalized search result from any provider."""

    source: str  # Provider name: "duckduckgo", "ticketmaster", "eventbrite"
    title: str  # Result title/heading
    snippet: str  # Brief description/summary
    url: Optional[str] = None  # Link to full information
    confidence: float = 0.7  # Confidence score (0.0-1.0)

    # Optional metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # Event-specific fields (optional)
    event_date: Optional[str] = None  # ISO 8601 format
    venue: Optional[str] = None
    location: Optional[str] = None
    price_range: Optional[str] = None

    # Source attribution
    retrieved_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for LLM context."""
        result = {
            "source": self.source,
            "title": self.title,
            "snippet": self.snippet,
        }

        if self.url:
            result["url"] = self.url

        if self.event_date:
            result["date"] = self.event_date

        if self.venue:
            result["venue"] = self.venue

        if self.location:
            result["location"] = self.location

        if self.price_range:
            result["price"] = self.price_range

        return result


class SearchProvider(ABC):
    """
    Abstract base class for all search providers.

    Each provider must implement:
    - search(): Execute search and return normalized results
    - name property: Provider identifier
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize search provider.

        Args:
            api_key: API key for the provider (if required)
        """
        self.api_key = api_key
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name identifier (e.g., 'ticketmaster', 'duckduckgo')."""
        pass

    @abstractmethod
    async def search(self, query: str, location: Optional[str] = None, **kwargs) -> List[SearchResult]:
        """
        Execute search query and return normalized results.

        Args:
            query: Search query string
            location: Optional location filter (city, state, etc.)
            **kwargs: Provider-specific parameters

        Returns:
            List of SearchResult objects

        Raises:
            Exception: If search fails (caller should handle gracefully)
        """
        pass

    def normalize_result(
        self,
        title: str,
        snippet: str,
        url: Optional[str] = None,
        confidence: float = 0.7,
        **kwargs
    ) -> SearchResult:
        """
        Helper to create normalized SearchResult.

        Args:
            title: Result title
            snippet: Result description
            url: Optional URL
            confidence: Confidence score (0.0-1.0)
            **kwargs: Additional metadata

        Returns:
            SearchResult object
        """
        return SearchResult(
            source=self.name,
            title=title,
            snippet=snippet,
            url=url,
            confidence=confidence,
            **kwargs
        )

    async def health_check(self) -> bool:
        """
        Check if provider is accessible.

        Returns:
            True if provider is healthy, False otherwise
        """
        try:
            # Simple test query
            results = await self.search("test", limit=1)
            return True
        except Exception as e:
            self.logger.warning(f"{self.name} health check failed: {e}")
            return False

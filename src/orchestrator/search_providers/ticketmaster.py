"""
Ticketmaster Discovery API provider.

Searches for concerts, sports events, and theater performances.
"""

import httpx
from typing import List, Optional
from urllib.parse import quote_plus
from datetime import datetime

from .base import SearchProvider, SearchResult


class TicketmasterProvider(SearchProvider):
    """
    Ticketmaster Discovery API provider.

    Advantages:
    - FREE (5,000 API calls/day)
    - Comprehensive event data (concerts, sports, theater)
    - Real-time availability and pricing
    - Venue and artist information
    - Geographic search capabilities

    Best for:
    - Concert queries
    - Sports events
    - Theater performances
    - Live entertainment
    """

    BASE_URL = "https://app.ticketmaster.com/discovery/v2"

    def __init__(self, api_key: str):
        """
        Initialize Ticketmaster provider.

        Args:
            api_key: Ticketmaster API key (required)
        """
        super().__init__(api_key=api_key)
        self.client = httpx.AsyncClient(timeout=10.0)

    @property
    def name(self) -> str:
        return "ticketmaster"

    async def search(
        self,
        query: str,
        location: Optional[str] = "Baltimore, MD",
        limit: int = 5,
        **kwargs
    ) -> List[SearchResult]:
        """
        Search Ticketmaster for events.

        Args:
            query: Search query (artist name, event type, etc.)
            location: City/state for search (default: Baltimore, MD)
            limit: Maximum number of results (default 5)
            **kwargs: Additional parameters:
                - classification_name: "music", "sports", "arts"
                - start_date: ISO 8601 date (YYYY-MM-DD)
                - radius: Search radius in miles (default 25)

        Returns:
            List of SearchResult objects with event details
        """
        try:
            self.logger.info(f"Ticketmaster search started: {query} in {location}")

            # Build request parameters
            params = {
                "apikey": self.api_key,
                "keyword": query,
                "size": limit,
                "sort": "date,asc"  # Sort by date
            }

            # Add location if provided
            if location:
                params["city"] = location

            # Add optional parameters
            if "classification_name" in kwargs:
                params["classificationName"] = kwargs["classification_name"]

            if "start_date" in kwargs:
                params["startDateTime"] = kwargs["start_date"]

            if "radius" in kwargs:
                params["radius"] = kwargs["radius"]
            else:
                params["radius"] = "25"  # Default 25 miles

            # Make API request
            url = f"{self.BASE_URL}/events.json"
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            results = []

            # Parse events
            embedded = data.get("_embedded", {})
            events = embedded.get("events", [])

            for event in events[:limit]:
                # Extract event details
                event_name = event.get("name", "Unknown Event")
                event_url = event.get("url", "")

                # Date information
                dates_info = event.get("dates", {})
                start_info = dates_info.get("start", {})
                event_date = start_info.get("localDate", "")
                event_time = start_info.get("localTime", "")

                # Venue information
                venues_list = event.get("_embedded", {}).get("venues", [])
                venue_name = venues_list[0].get("name", "") if venues_list else ""
                venue_city = venues_list[0].get("city", {}).get("name", "") if venues_list else ""
                venue_state = venues_list[0].get("state", {}).get("stateCode", "") if venues_list else ""

                venue_location = f"{venue_city}, {venue_state}" if venue_city and venue_state else location

                # Price information
                price_ranges = event.get("priceRanges", [])
                price_range = None
                if price_ranges:
                    min_price = price_ranges[0].get("min", 0)
                    max_price = price_ranges[0].get("max", 0)
                    currency = price_ranges[0].get("currency", "USD")
                    price_range = f"${min_price:.2f} - ${max_price:.2f} {currency}"

                # Build event description
                datetime_str = f"{event_date}"
                if event_time:
                    datetime_str += f" at {event_time}"

                snippet_parts = [event_name]
                if venue_name:
                    snippet_parts.append(f"at {venue_name}")
                if datetime_str:
                    snippet_parts.append(f"on {datetime_str}")
                if price_range:
                    snippet_parts.append(f"(Tickets: {price_range})")

                snippet = " ".join(snippet_parts)

                # Create normalized result
                result = self.normalize_result(
                    title=event_name,
                    snippet=snippet,
                    url=event_url,
                    confidence=1.0,  # High confidence for official event data
                    event_date=event_date,
                    venue=venue_name,
                    location=venue_location,
                    price_range=price_range,
                    metadata={
                        "event_time": event_time,
                        "event_id": event.get("id", ""),
                        "classification": event.get("classifications", [{}])[0].get("segment", {}).get("name", ""),
                        "status": dates_info.get("status", {}).get("code", "")
                    }
                )
                results.append(result)

            self.logger.info(f"Ticketmaster search completed: {len(results)} events found")

            return results

        except httpx.HTTPStatusError as e:
            self.logger.error(f"Ticketmaster HTTP error: {e}, status={e.response.status_code}")
            if e.response.status_code == 401:
                self.logger.error("Ticketmaster API key invalid or unauthorized")
            raise
        except httpx.RequestError as e:
            self.logger.error(f"Ticketmaster request error: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Ticketmaster search failed: {e}")
            raise

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

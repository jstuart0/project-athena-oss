"""
Eventbrite API provider.

Searches for local community events, meetups, and workshops.
"""

import httpx
from typing import List, Optional
from urllib.parse import quote_plus
from datetime import datetime

from .base import SearchProvider, SearchResult


class EventbriteProvider(SearchProvider):
    """
    Eventbrite API v3 provider.

    Advantages:
    - FREE (1,000 API calls/day)
    - Local community events
    - Meetups and workshops
    - Conferences and networking events
    - Detailed venue and organizer information

    Best for:
    - Local community events
    - Meetups and workshops
    - Conferences
    - Educational events
    """

    BASE_URL = "https://www.eventbriteapi.com/v3"

    def __init__(self, api_key: str):
        """
        Initialize Eventbrite provider.

        Args:
            api_key: Eventbrite API key (required)
        """
        super().__init__(api_key=api_key)
        self.client = httpx.AsyncClient(
            timeout=10.0,
            headers={"Authorization": f"Bearer {api_key}"}
        )

    @property
    def name(self) -> str:
        return "eventbrite"

    async def search(
        self,
        query: str,
        location: Optional[str] = "Baltimore, MD",
        limit: int = 5,
        **kwargs
    ) -> List[SearchResult]:
        """
        Search Eventbrite for events.

        Args:
            query: Search query (event type, keywords, etc.)
            location: City/location for search (default: Baltimore, MD)
            limit: Maximum number of results (default 5)
            **kwargs: Additional parameters:
                - start_date: ISO 8601 date range start
                - end_date: ISO 8601 date range end
                - categories: Event category IDs
                - price: "free", "paid"

        Returns:
            List of SearchResult objects with event details
        """
        try:
            self.logger.info(f"Eventbrite search started: {query} in {location}")

            # Build request parameters
            params = {
                "q": query,
                "location.address": location,
                "expand": "venue,ticket_availability",
                "sort_by": "date"
            }

            # Add optional parameters
            if "start_date" in kwargs:
                params["start_date.range_start"] = kwargs["start_date"]

            if "end_date" in kwargs:
                params["start_date.range_end"] = kwargs["end_date"]

            if "categories" in kwargs:
                params["categories"] = kwargs["categories"]

            if "price" in kwargs:
                params["price"] = kwargs["price"]

            # Make API request
            url = f"{self.BASE_URL}/events/search/"
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            results = []

            # Parse events
            events = data.get("events", [])

            for event in events[:limit]:
                # Extract event details
                event_name = event.get("name", {}).get("text", "Unknown Event")
                event_description = event.get("description", {}).get("text", "")
                event_url = event.get("url", "")

                # Date information
                start_info = event.get("start", {})
                event_date = ""
                event_time = ""

                if start_info.get("local"):
                    # Parse ISO 8601 datetime
                    local_datetime = start_info.get("local", "")
                    try:
                        dt = datetime.fromisoformat(local_datetime.replace("Z", "+00:00"))
                        event_date = dt.strftime("%Y-%m-%d")
                        event_time = dt.strftime("%I:%M %p")
                    except:
                        event_date = local_datetime.split("T")[0] if "T" in local_datetime else local_datetime

                # Venue information
                venue_info = event.get("venue", {})
                venue_name = venue_info.get("name", "")
                venue_address = venue_info.get("address", {})
                venue_city = venue_address.get("city", "")
                venue_state = venue_address.get("region", "")

                venue_location = f"{venue_city}, {venue_state}" if venue_city and venue_state else location

                # Price information
                is_free = event.get("is_free", False)
                price_range = "Free" if is_free else "Paid"

                # Ticket availability
                ticket_info = event.get("ticket_availability", {})
                is_sold_out = ticket_info.get("is_sold_out", False)
                if is_sold_out:
                    price_range = "Sold Out"

                # Build event description
                snippet_parts = [event_name]

                if venue_name:
                    snippet_parts.append(f"at {venue_name}")

                datetime_str = ""
                if event_date:
                    datetime_str = event_date
                    if event_time:
                        datetime_str += f" at {event_time}"
                    snippet_parts.append(f"on {datetime_str}")

                snippet_parts.append(f"({price_range})")

                # Add description snippet (first 100 chars)
                if event_description:
                    desc_preview = event_description[:100].strip()
                    if len(event_description) > 100:
                        desc_preview += "..."
                    snippet_parts.append(f"- {desc_preview}")

                snippet = " ".join(snippet_parts)

                # Create normalized result
                result = self.normalize_result(
                    title=event_name,
                    snippet=snippet,
                    url=event_url,
                    confidence=0.9,  # High confidence for official event data
                    event_date=event_date,
                    venue=venue_name,
                    location=venue_location,
                    price_range=price_range,
                    metadata={
                        "event_time": event_time,
                        "event_id": event.get("id", ""),
                        "organizer": event.get("organizer", {}).get("name", ""),
                        "capacity": event.get("capacity", 0),
                        "is_free": is_free,
                        "is_sold_out": is_sold_out,
                        "category": event.get("category", {}).get("name", "")
                    }
                )
                results.append(result)

            self.logger.info(f"Eventbrite search completed: {len(results)} events found")

            return results

        except httpx.HTTPStatusError as e:
            self.logger.error(f"Eventbrite HTTP error: {e}, status={e.response.status_code}")
            if e.response.status_code == 401:
                self.logger.error("Eventbrite API key invalid or unauthorized")
            raise
        except httpx.RequestError as e:
            self.logger.error(f"Eventbrite request error: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Eventbrite search failed: {e}")
            raise

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

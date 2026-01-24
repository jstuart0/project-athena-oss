"""
Orchestrator Helper Functions

Utility functions for date extraction, model selection, and other common operations.
"""

import re
from datetime import datetime
from typing import Optional

from shared.admin_config import get_admin_client
from shared.logging_config import configure_logging

from .constants import FALLBACK_MODELS, _DEFAULT_MODEL

logger = configure_logging("orchestrator.utils")


def extract_date_from_query(query: str) -> Optional[tuple]:
    """
    Extract a specific date from a natural language query.

    Returns tuple of (date_str_display, date_str_api) or None if no specific date found.
    - date_str_display: Human readable format like "Saturday, December 6, 2025"
    - date_str_api: API format like "2025-12-06"
    """
    query_lower = query.lower()
    today = datetime.now()
    current_year = today.year

    # Month name mapping
    months = {
        'january': 1, 'jan': 1,
        'february': 2, 'feb': 2,
        'march': 3, 'mar': 3,
        'april': 4, 'apr': 4,
        'may': 5,
        'june': 6, 'jun': 6,
        'july': 7, 'jul': 7,
        'august': 8, 'aug': 8,
        'september': 9, 'sep': 9, 'sept': 9,
        'october': 10, 'oct': 10,
        'november': 11, 'nov': 11,
        'december': 12, 'dec': 12
    }

    # Pattern 1: "December 6th", "Dec 6", "December 6"
    pattern1 = r'\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sep|sept|october|oct|november|nov|december|dec)\s+(\d{1,2})(?:st|nd|rd|th)?\b'
    match = re.search(pattern1, query_lower)
    if match:
        month_name = match.group(1)
        day = int(match.group(2))
        month = months.get(month_name)
        if month and 1 <= day <= 31:
            # Determine year - if the date is in the past, use next year
            target_date = datetime(current_year, month, day)
            if target_date < today:
                target_date = datetime(current_year + 1, month, day)

            display_str = target_date.strftime("%A, %B %d, %Y")
            api_str = target_date.strftime("%Y-%m-%d")
            return (display_str, api_str)

    # Pattern 2: "6th of December", "6 December"
    pattern2 = r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sep|sept|october|oct|november|nov|december|dec)\b'
    match = re.search(pattern2, query_lower)
    if match:
        day = int(match.group(1))
        month_name = match.group(2)
        month = months.get(month_name)
        if month and 1 <= day <= 31:
            target_date = datetime(current_year, month, day)
            if target_date < today:
                target_date = datetime(current_year + 1, month, day)

            display_str = target_date.strftime("%A, %B %d, %Y")
            api_str = target_date.strftime("%Y-%m-%d")
            return (display_str, api_str)

    # Pattern 3: MM/DD or MM-DD
    pattern3 = r'\b(\d{1,2})[/-](\d{1,2})\b'
    match = re.search(pattern3, query_lower)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            target_date = datetime(current_year, month, day)
            if target_date < today:
                target_date = datetime(current_year + 1, month, day)

            display_str = target_date.strftime("%A, %B %d, %Y")
            api_str = target_date.strftime("%Y-%m-%d")
            return (display_str, api_str)

    return None


async def get_model_for_component(component_name: str) -> str:
    """Get model for a component from database, with fallback."""
    try:
        admin_client = get_admin_client()
        config = await admin_client.get_component_model(component_name)

        if config and config.get("enabled"):
            return config.get("model_name")
    except Exception as e:
        logger.warning(f"Failed to get model for {component_name}: {e}")

    # Return fallback
    return FALLBACK_MODELS.get(component_name, _DEFAULT_MODEL)


def get_location_state(city: str) -> Optional[str]:
    """
    Get the state abbreviation for a city.

    Args:
        city: City name

    Returns:
        State abbreviation or None if not found
    """
    from .constants import CITY_STATE_MAP
    return CITY_STATE_MAP.get(city)


def format_tool_result(tool_name: str, result: dict, error: Optional[str] = None) -> dict:
    """
    Format a tool execution result for consistent output.

    Args:
        tool_name: Name of the tool that was called
        result: The result data from the tool
        error: Optional error message if tool failed

    Returns:
        Formatted result dict with tool_name, success, data/error fields
    """
    if error:
        return {
            "tool_name": tool_name,
            "success": False,
            "error": error,
            "data": None
        }
    return {
        "tool_name": tool_name,
        "success": True,
        "error": None,
        "data": result
    }


def normalize_location(location: str) -> str:
    """
    Normalize a location string by adding state if known city.

    Args:
        location: Location string (e.g., "Baltimore" or "Baltimore, MD")

    Returns:
        Normalized location (e.g., "Baltimore, MD")
    """
    from .constants import CITY_STATE_MAP

    # If already has state, return as-is
    if "," in location:
        return location

    # Try to find state for city
    city = location.strip()
    state = CITY_STATE_MAP.get(city)
    if state:
        return f"{city}, {state}"

    return location


def is_control_intent(query: str) -> bool:
    """
    Check if a query appears to be a smart home control command.

    Args:
        query: The user query

    Returns:
        True if query contains control patterns
    """
    from .constants import CONTROL_PATTERNS

    query_lower = query.lower()
    return any(pattern in query_lower for pattern in CONTROL_PATTERNS)

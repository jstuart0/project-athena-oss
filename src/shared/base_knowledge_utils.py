"""
Base Knowledge Utilities

Provides functions to format and inject base knowledge context into LLM prompts.
Handles dynamic placeholders like {dynamic:current_date} and {dynamic:current_time}.
"""
from datetime import datetime
from typing import List, Dict, Any
import structlog

logger = structlog.get_logger()


def resolve_dynamic_value(value: str) -> str:
    """
    Resolve dynamic placeholders in knowledge values.

    Supported placeholders:
    - {dynamic:current_date} -> "Monday, November 24, 2025"
    - {dynamic:current_time} -> "2:45 PM"

    Args:
        value: Knowledge value that may contain dynamic placeholders

    Returns:
        Value with all placeholders resolved
    """
    if "{dynamic:" not in value:
        return value

    now = datetime.now()

    # Replace dynamic placeholders
    value = value.replace(
        "{dynamic:current_date}",
        now.strftime("%A, %B %d, %Y")
    )
    value = value.replace(
        "{dynamic:current_time}",
        now.strftime("%-I:%M %p")
    )

    return value


def build_knowledge_context(knowledge_entries: List[Dict[str, Any]]) -> str:
    """
    Build formatted context string from base knowledge entries.

    Entries should already be filtered by applies_to and sorted by priority.

    Args:
        knowledge_entries: List of knowledge entries from Admin API

    Returns:
        Formatted context string ready for injection into system prompt
    """
    if not knowledge_entries:
        return ""

    context_lines = ["CONTEXT INFORMATION:"]

    for entry in knowledge_entries:
        # Get value and resolve any dynamic placeholders
        value = entry.get("value", "")
        resolved_value = resolve_dynamic_value(value)

        # Format based on category
        category = entry.get("category", "general")

        if category == "property":
            context_lines.append(f"• Property: {resolved_value}")
        elif category == "location":
            key = entry.get("key", "")
            if "default" in key:
                context_lines.append(f"• Default Location: {resolved_value}")
            else:
                context_lines.append(f"• Location: {resolved_value}")
        elif category == "user":
            # User context is crucial - make it prominent
            key = entry.get("key", "")
            if key in ("owner_name", "guest_name"):
                context_lines.append(f"• The user's name is: {resolved_value}")
            else:
                context_lines.append(f"• User Context: {resolved_value}")
        elif category == "temporal":
            key = entry.get("key", "")
            if "date" in key:
                context_lines.append(f"• Current Date: {resolved_value}")
            elif "time" in key:
                context_lines.append(f"• Current Time: {resolved_value}")
            else:
                context_lines.append(f"• {resolved_value}")
        elif category == "general":
            key = entry.get("key", "")
            if "assistant_name" in key:
                context_lines.append(f"• Your Name: {resolved_value}")
            elif "location_context" in key:
                context_lines.append(f"• {resolved_value}")
            else:
                context_lines.append(f"• {resolved_value}")
        else:
            # Generic formatting for unknown categories
            context_lines.append(f"• {resolved_value}")

    # Join with newlines and add trailing newline
    context = "\n".join(context_lines)
    context += "\n\n"

    logger.info(
        "base_knowledge_context_built",
        entry_count=len(knowledge_entries),
        context_length=len(context)
    )

    return context


def extract_home_address(knowledge_entries: List[Dict[str, Any]]) -> str:
    """
    Extract the home/property address from base knowledge entries.

    Looks for entries with category="property" and key="address" to find
    the user's home address for proximity queries.

    Args:
        knowledge_entries: List of knowledge entries from Admin API

    Returns:
        The home address string, or "Baltimore, MD" as fallback
    """
    if not knowledge_entries:
        return "Baltimore, MD"

    # Look for property address entry
    for entry in knowledge_entries:
        category = entry.get("category", "")
        key = entry.get("key", "")
        value = entry.get("value", "")

        if category == "property" and key == "address" and value:
            logger.info(
                "home_address_extracted",
                address=value
            )
            return value

    # Fallback to default location entries
    for entry in knowledge_entries:
        category = entry.get("category", "")
        key = entry.get("key", "")
        value = entry.get("value", "")

        if category == "location" and "default" in key and value:
            logger.info(
                "default_location_extracted",
                location=value
            )
            return value

    logger.warning("no_home_address_found_using_fallback")
    return "Baltimore, MD"


async def get_home_address_for_user(admin_client, user_mode: str = "guest") -> str:
    """
    Fetch the home address for a specific user mode.

    Used for proximity queries like "near me" or "closest to my house".

    Args:
        admin_client: AdminConfigClient instance
        user_mode: User mode ('guest', 'owner', 'both')

    Returns:
        Home address string for location-based queries
    """
    try:
        knowledge_entries = await admin_client.get_base_knowledge(
            applies_to=user_mode,
            enabled_only=True
        )

        if not knowledge_entries:
            logger.info("no_base_knowledge_for_home_address", user_mode=user_mode)
            return "Baltimore, MD"

        return extract_home_address(knowledge_entries)

    except Exception as e:
        logger.error(
            "failed_to_get_home_address",
            user_mode=user_mode,
            error=str(e)
        )
        return "Baltimore, MD"


async def get_knowledge_context_for_user(admin_client, user_mode: str = "guest") -> str:
    """
    Fetch and format base knowledge context for a specific user mode.

    Args:
        admin_client: AdminConfigClient instance
        user_mode: User mode ('guest', 'owner', 'both')

    Returns:
        Formatted context string ready for system prompt injection
    """
    try:
        # Fetch knowledge entries filtered by user mode
        knowledge_entries = await admin_client.get_base_knowledge(
            applies_to=user_mode,
            enabled_only=True
        )

        if not knowledge_entries:
            logger.info("no_base_knowledge_entries_found", user_mode=user_mode)
            return ""

        # Build formatted context
        context = build_knowledge_context(knowledge_entries)

        logger.info(
            "knowledge_context_generated",
            user_mode=user_mode,
            entry_count=len(knowledge_entries),
            context_length=len(context)
        )

        return context

    except Exception as e:
        logger.error(
            "failed_to_build_knowledge_context",
            user_mode=user_mode,
            error=str(e)
        )
        return ""


if __name__ == "__main__":
    # Test dynamic value resolution
    test_values = [
        "{dynamic:current_date}",
        "{dynamic:current_time}",
        "The current date is {dynamic:current_date} and the time is {dynamic:current_time}",
        "No dynamic values here"
    ]

    print("Testing dynamic value resolution:")
    for test_val in test_values:
        resolved = resolve_dynamic_value(test_val)
        print(f"  Input:  {test_val}")
        print(f"  Output: {resolved}")
        print()

    # Test context building
    test_knowledge = [
        {
            "category": "property",
            "key": "address",
            "value": "912 S Clinton St, Baltimore, MD 21224",
            "priority": 100
        },
        {
            "category": "user",
            "key": "user_type",
            "value": "You are an Airbnb guest staying at this property",
            "priority": 95
        },
        {
            "category": "temporal",
            "key": "current_date",
            "value": "{dynamic:current_date}",
            "priority": 80
        },
        {
            "category": "general",
            "key": "assistant_name",
            "value": "Athena",
            "priority": 70
        }
    ]

    print("\nTesting context building:")
    context = build_knowledge_context(test_knowledge)
    print(context)

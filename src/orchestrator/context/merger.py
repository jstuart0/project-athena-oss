"""
Context Merger

Merges new query information with previous conversation context.
"""

from typing import Dict, Any

from orchestrator.state import ConversationContext


def merge_with_context(
    new_query: str,
    new_entities: Dict[str, Any],
    context: ConversationContext,
    ref_info: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Merge new query info with previous context based on reference type.

    This allows follow-up queries to inherit information from previous turns:
    - "turn them off" inherits the entities from "turn on the kitchen lights"
    - "make it brighter" keeps the same target with a brightness adjustment
    - "what about tomorrow?" keeps the same query type with temporal update

    Args:
        new_query: The current query text
        new_entities: Entities extracted from current query
        context: Previous conversation context
        ref_info: Context reference detection info from detector

    Returns:
        Dict containing:
        - entities: Merged entities (context + new)
        - parameters: Merged parameters with adjustments
        - intent: The intent to use (usually from context)
    """
    merged = {
        "entities": context.entities.copy(),
        "parameters": context.parameters.copy(),
        "intent": context.intent,
    }

    # If new query has explicit entities, use them (override context)
    for key, value in new_entities.items():
        if value:  # Only override if new value is not None/empty
            merged["entities"][key] = value

    # Handle specific reference types
    if "modifier" in ref_info["ref_types"]:
        # Modifiers adjust parameters but keep same target
        query_lower = new_query.lower()
        if "brighter" in query_lower:
            merged["parameters"]["brightness_adjust"] = "increase"
        elif "dimmer" in query_lower:
            merged["parameters"]["brightness_adjust"] = "decrease"
        elif "different color" in query_lower:
            merged["parameters"]["color_change"] = True

    if "temporal" in ref_info["ref_types"]:
        # Temporal references update the time but keep the query type
        query_lower = new_query.lower()
        if "tomorrow" in query_lower:
            merged["entities"]["time_ref"] = "tomorrow"
        elif "this weekend" in query_lower:
            merged["entities"]["time_ref"] = "this_weekend"
        elif "next week" in query_lower:
            merged["entities"]["time_ref"] = "next_week"

    return merged

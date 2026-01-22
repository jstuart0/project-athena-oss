"""
Home Assistant Status Query Optimizer

Optimizes status queries like "what lights are on?" with:
1. Bulk state queries - single API call instead of per-entity
2. Entity type filtering - only query relevant domains
3. Skip synthesis - return templated response without LLM

These optimizations can save 3-5 seconds on status queries.
"""

import re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
import structlog

logger = structlog.get_logger()


@dataclass
class StatusQueryResult:
    """Result of an optimized status query."""
    query_type: str  # lights, locks, doors, climate, etc.
    entities: List[Dict[str, Any]]
    summary: str  # Pre-formatted summary
    skip_synthesis: bool  # Whether to skip LLM synthesis
    raw_states: Dict[str, Any]  # Raw state data for LLM if needed


# Entity type mappings - which HA domains to query for each type
ENTITY_TYPE_MAP = {
    "lights": ["light"],
    "locks": ["lock"],
    "doors": ["lock", "binary_sensor"],  # Doors can be locks or door sensors
    "fans": ["fan"],
    "climate": ["climate"],
    "thermostats": ["climate"],
    "temperature": ["climate", "sensor"],
    "windows": ["binary_sensor"],
    "sensors": ["sensor", "binary_sensor"],
    "switches": ["switch"],
    "covers": ["cover"],  # Garage doors, blinds
    "garage": ["cover"],
    "media": ["media_player"],
}

# Patterns for detecting status query types
STATUS_PATTERNS = {
    "lights_on": [
        r"what lights? (?:are|is) (?:currently )?on",
        r"which lights? (?:are|is) on",
        r"are (?:any|the) lights? on",
        r"lights? (?:that are )?on",
        r"show (?:me )?(?:the )?lights? on",
        r"any lights? left on",
        r"what's? lit",
    ],
    "lights_off": [
        r"what lights? (?:are|is) (?:currently )?off",
        r"which lights? (?:are|is) off",
        r"are (?:any|the) lights? off",
    ],
    "locks_status": [
        r"(?:is|are) (?:the |my )?(?:door|doors|lock|locks) locked",
        r"(?:is|are) (?:the |my )?(?:door|doors|lock|locks) unlocked",
        r"(?:door|lock) status",
        r"check (?:the )?(?:door|lock)",
        r"are (?:all )?(?:my )?doors? (?:locked|secure)",
        r"did i lock",
        r"doors? good",
    ],
    "climate_status": [
        r"what(?:'s| is) the (?:temperature|temp) (?:inside|in here|in the house)",
        r"(?:current |indoor )?temp(?:erature)?",
        r"thermostat (?:status|setting)",
        r"what(?:'s| is) the thermostat (?:set to|at)",
        r"how (?:warm|cold|hot) is it",
        r"what temp (?:are )?we at",
    ],
    "general_status": [
        r"what(?:'s| is) (?:on|running|active)",
        r"status (?:of |check)",
        r"what (?:devices? )?(?:are|is) on",
    ],
}


def detect_status_query_type(query: str) -> Optional[str]:
    """
    Detect if a query is a status query and what type.

    Returns:
        Query type string (e.g., "lights_on") or None if not a status query
    """
    query_lower = query.lower().strip()

    for query_type, patterns in STATUS_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, query_lower):
                logger.debug("status_query_detected", query_type=query_type, pattern=pattern)
                return query_type

    return None


def get_domains_for_query_type(query_type: str) -> List[str]:
    """Get HA domains to query for a status query type."""
    if query_type.startswith("lights"):
        return ENTITY_TYPE_MAP["lights"]
    elif query_type.startswith("locks") or query_type.startswith("doors"):
        return ENTITY_TYPE_MAP["locks"]
    elif query_type.startswith("climate"):
        return ENTITY_TYPE_MAP["climate"]
    else:
        # General status - return common domains
        return ["light", "lock", "climate", "cover"]


def filter_entities_by_domains(
    all_entities: Dict[str, Any],
    domains: List[str]
) -> Dict[str, Any]:
    """
    Filter entities to only include specified domains.

    Args:
        all_entities: Dict of entity_id -> entity state
        domains: List of domain prefixes (e.g., ["light", "lock"])

    Returns:
        Filtered dict of entities
    """
    filtered = {}
    for entity_id, entity in all_entities.items():
        domain = entity_id.split('.')[0]
        if domain in domains:
            filtered[entity_id] = entity

    logger.debug(
        "entities_filtered",
        total=len(all_entities),
        filtered=len(filtered),
        domains=domains
    )
    return filtered


def filter_entities_by_state(
    entities: Dict[str, Any],
    target_state: str
) -> List[Dict[str, Any]]:
    """
    Filter entities by their current state.

    Args:
        entities: Dict of entity_id -> entity state
        target_state: State to filter for (e.g., "on", "locked")

    Returns:
        List of entity dicts matching the state
    """
    matches = []
    for entity_id, entity in entities.items():
        state = entity.get("state", "").lower()
        if state == target_state.lower():
            attrs = entity.get("attributes", {})
            matches.append({
                "entity_id": entity_id,
                "state": state,
                "friendly_name": attrs.get("friendly_name", entity_id),
            })

    return matches


def format_lights_on_response(entities: List[Dict]) -> str:
    """Format a response for 'what lights are on' query."""
    if not entities:
        return "All lights are off."

    count = len(entities)
    if count == 1:
        name = entities[0].get("friendly_name", entities[0]["entity_id"])
        return f"One light is on: {name}."

    # Get friendly names
    names = [e.get("friendly_name", e["entity_id"]) for e in entities]

    if count <= 5:
        # List all names
        names_str = ", ".join(names[:-1]) + f", and {names[-1]}"
        return f"{count} lights are on: {names_str}."
    else:
        # Too many - just give count and first few
        first_few = ", ".join(names[:3])
        return f"{count} lights are on, including {first_few}, and {count - 3} more."


def format_lights_off_response(entities: List[Dict], total_lights: int) -> str:
    """Format a response for 'what lights are off' query."""
    if not entities:
        return "All lights are on."

    count = len(entities)
    if count == total_lights:
        return "All lights are off."

    if count == 1:
        name = entities[0].get("friendly_name", entities[0]["entity_id"])
        return f"One light is off: {name}."

    names = [e.get("friendly_name", e["entity_id"]) for e in entities]

    if count <= 5:
        names_str = ", ".join(names[:-1]) + f", and {names[-1]}"
        return f"{count} lights are off: {names_str}."
    else:
        first_few = ", ".join(names[:3])
        return f"{count} lights are off, including {first_few}, and {count - 3} more."


def format_locks_response(entities: List[Dict], query_type: str) -> str:
    """Format a response for lock status queries."""
    locked = [e for e in entities if e["state"] == "locked"]
    unlocked = [e for e in entities if e["state"] == "unlocked"]

    if not entities:
        return "I couldn't find any lock devices."

    all_locked = len(unlocked) == 0
    all_unlocked = len(locked) == 0

    if all_locked:
        if len(locked) == 1:
            return f"Yes, {locked[0].get('friendly_name', 'the door')} is locked."
        return f"Yes, all {len(locked)} doors are locked."

    if all_unlocked:
        if len(unlocked) == 1:
            return f"No, {unlocked[0].get('friendly_name', 'the door')} is unlocked."
        return f"No, all {len(unlocked)} doors are unlocked."

    # Mixed state
    locked_names = [e.get("friendly_name", e["entity_id"]) for e in locked]
    unlocked_names = [e.get("friendly_name", e["entity_id"]) for e in unlocked]

    return f"{len(locked)} locked ({', '.join(locked_names)}), {len(unlocked)} unlocked ({', '.join(unlocked_names)})."


def format_climate_response(entities: List[Dict]) -> str:
    """Format a response for climate/thermostat status queries."""
    if not entities:
        return "I couldn't find any thermostat devices."

    # Usually just one thermostat
    entity = entities[0]
    attrs = entity.get("attributes", {})

    current_temp = attrs.get("current_temperature")
    target_temp = attrs.get("temperature")
    target_high = attrs.get("target_temp_high")
    target_low = attrs.get("target_temp_low")
    hvac_mode = entity.get("state", "off")
    hvac_action = attrs.get("hvac_action", "idle")

    parts = []

    if current_temp:
        parts.append(f"It's currently {current_temp}°F inside")

    if hvac_mode == "heat_cool" and target_low and target_high:
        parts.append(f"set to {target_low}-{target_high}°F")
    elif target_temp:
        parts.append(f"set to {target_temp}°F")

    if hvac_action and hvac_action != "idle":
        parts.append(f"({hvac_action})")

    if not parts:
        return f"Thermostat is {hvac_mode}."

    return ". ".join(parts) + "."


async def optimize_status_query(
    query: str,
    entity_manager,  # HAEntityManager instance
    feature_config: Dict[str, Any] = None
) -> Optional[StatusQueryResult]:
    """
    Optimize a status query using bulk queries and entity filtering.

    Args:
        query: The user query
        entity_manager: HAEntityManager instance for state queries
        feature_config: Feature configuration from admin

    Returns:
        StatusQueryResult if optimization applied, None if should use normal flow
    """
    # Detect query type
    query_type = detect_status_query_type(query)
    if not query_type:
        return None

    logger.info("status_query_optimizing", query_type=query_type, query=query[:50])

    # Get relevant domains
    domains = get_domains_for_query_type(query_type)

    # Get all entities (uses cache if available)
    all_entities = await entity_manager.get_entities()

    # Filter by domain
    filtered_entities = filter_entities_by_domains(all_entities, domains)

    # Get max entities from config
    max_entities = 50
    if feature_config and "batch_size" in feature_config:
        max_entities = feature_config["batch_size"]

    # Determine skip_synthesis based on config
    skip_synthesis = True
    if feature_config and "skip_synthesis" in feature_config:
        skip_synthesis = feature_config["skip_synthesis"]

    # Process based on query type
    if query_type == "lights_on":
        on_lights = filter_entities_by_state(filtered_entities, "on")[:max_entities]
        summary = format_lights_on_response(on_lights)
        return StatusQueryResult(
            query_type=query_type,
            entities=on_lights,
            summary=summary,
            skip_synthesis=skip_synthesis,
            raw_states=filtered_entities
        )

    elif query_type == "lights_off":
        off_lights = filter_entities_by_state(filtered_entities, "off")[:max_entities]
        total_lights = len(filtered_entities)
        summary = format_lights_off_response(off_lights, total_lights)
        return StatusQueryResult(
            query_type=query_type,
            entities=off_lights,
            summary=summary,
            skip_synthesis=skip_synthesis,
            raw_states=filtered_entities
        )

    elif query_type == "locks_status":
        lock_entities = []
        for entity_id, entity in filtered_entities.items():
            if entity_id.startswith("lock."):
                attrs = entity.get("attributes", {})
                lock_entities.append({
                    "entity_id": entity_id,
                    "state": entity.get("state", "unknown"),
                    "friendly_name": attrs.get("friendly_name", entity_id),
                })

        summary = format_locks_response(lock_entities, query_type)
        return StatusQueryResult(
            query_type=query_type,
            entities=lock_entities,
            summary=summary,
            skip_synthesis=skip_synthesis,
            raw_states=filtered_entities
        )

    elif query_type == "climate_status":
        climate_entities = []
        for entity_id, entity in filtered_entities.items():
            if entity_id.startswith("climate."):
                climate_entities.append({
                    "entity_id": entity_id,
                    "state": entity.get("state", "unknown"),
                    "attributes": entity.get("attributes", {}),
                })

        summary = format_climate_response(climate_entities)
        return StatusQueryResult(
            query_type=query_type,
            entities=climate_entities,
            summary=summary,
            skip_synthesis=skip_synthesis,
            raw_states=filtered_entities
        )

    else:
        # General status - return without skip_synthesis for LLM to handle
        return StatusQueryResult(
            query_type=query_type,
            entities=[],
            summary="",
            skip_synthesis=False,
            raw_states=filtered_entities
        )


def should_skip_synthesis(
    result: StatusQueryResult,
    feature_enabled: bool = True
) -> Tuple[bool, str]:
    """
    Determine if synthesis should be skipped and return the response.

    Args:
        result: StatusQueryResult from optimize_status_query
        feature_enabled: Whether skip_synthesis feature is enabled

    Returns:
        Tuple of (should_skip, response_text)
    """
    if not feature_enabled:
        return (False, "")

    if not result.skip_synthesis:
        return (False, "")

    if not result.summary:
        return (False, "")

    logger.info(
        "synthesis_skipped",
        query_type=result.query_type,
        entity_count=len(result.entities)
    )

    return (True, result.summary)

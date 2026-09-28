"""
Simple command detection and execution for HA voice fast-path.

Detects patterns like:
- "Turn on/off [device]"
- "Set [device] to [value]"
- "What time is it?"
- "Good morning/night"

Executes directly against HA API without orchestrator.
"""

import re
import os
from typing import Optional, Tuple
from datetime import datetime
import httpx
import structlog

from gateway.mode_gate import fast_path_allowed

logger = structlog.get_logger("gateway.simple_commands")

# Simple command patterns
PATTERNS = {
    "turn_on": re.compile(r"(?:turn|switch)\s+on\s+(?:the\s+)?(.+)", re.IGNORECASE),
    "turn_off": re.compile(r"(?:turn|switch)\s+off\s+(?:the\s+)?(.+)", re.IGNORECASE),
    "time": re.compile(r"what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it", re.IGNORECASE),
    "date": re.compile(r"what(?:'s|\s+is)\s+(?:the\s+)?(?:today'?s?\s+)?date|what\s+day\s+is\s+(?:it|today)", re.IGNORECASE),
    "greeting_morning": re.compile(r"good\s+morning", re.IGNORECASE),
    "greeting_night": re.compile(r"good\s+night", re.IGNORECASE),
    "greeting_hello": re.compile(r"^(?:hello|hi|hey)(?:\s+(?:there|jarvis|athena))?[.!?]?$", re.IGNORECASE),
    "thank_you": re.compile(r"^(?:thanks?(?:\s+you)?|thank\s+you(?:\s+(?:very\s+much|so\s+much))?)[.!?]?$", re.IGNORECASE),
}

# Device name to entity_id mapping (common patterns)
DEVICE_MAPPINGS = {
    "kitchen lights": "light.kitchen",
    "kitchen light": "light.kitchen",
    "kitchen": "light.kitchen",
    "living room lights": "light.living_room",
    "living room light": "light.living_room",
    "living room": "light.living_room",
    "bedroom lights": "light.bedroom",
    "bedroom light": "light.bedroom",
    "bedroom": "light.bedroom",
    "office lights": "light.office",
    "office light": "light.office",
    "office": "light.office",
    "master bedroom lights": "light.master_bedroom",
    "master bedroom light": "light.master_bedroom",
    "master bedroom": "light.master_bedroom",
    "dining room lights": "light.dining_room",
    "dining room light": "light.dining_room",
    "dining room": "light.dining_room",
    "bathroom lights": "light.bathroom",
    "bathroom light": "light.bathroom",
    "bathroom": "light.bathroom",
    "hallway lights": "light.hallway",
    "hallway light": "light.hallway",
    "hallway": "light.hallway",
    "all lights": "light.all",
    "lights": "light.all",
}


async def detect_simple_command(query: str) -> Optional[Tuple[str, dict]]:
    """
    Detect if query is a simple command.

    Returns:
        Tuple of (command_type, params) if simple command detected
        None if query should go to orchestrator
    """
    query = query.strip()

    # Check each pattern
    for pattern_name, pattern in PATTERNS.items():
        match = pattern.match(query) if pattern_name not in ["turn_on", "turn_off"] else pattern.match(query)
        if match:
            if pattern_name == "turn_on":
                return ("turn_on", {"device": match.group(1).strip()})
            elif pattern_name == "turn_off":
                return ("turn_off", {"device": match.group(1).strip()})
            elif pattern_name in ["time", "date"]:
                return (pattern_name, {})
            elif pattern_name.startswith("greeting"):
                return ("greeting", {"type": pattern_name})
            elif pattern_name == "thank_you":
                return ("thank_you", {})

    return None


async def execute_simple_command(
    command_type: str,
    params: dict,
    ha_client: Optional[httpx.AsyncClient],
    ha_url: str,
    ha_token: str
) -> Optional[str]:
    """
    Execute simple command directly against HA API.

    ATHENA-69 D17: for turn_on/turn_off, this bypasses the orchestrator (and
    therefore every server-derived permission check) to call HA directly.
    Two independent gates apply before any HA call: the resolved entity must
    be a light (CONTROL_DEVICE_DOMAINS parity with the orchestrator's
    whole-house scope), and the mode service must confirm the house is
    currently in owner mode (fast_path_allowed(), fail-closed). Either gate
    failing returns None -- the caller already falls through to the
    orchestrator, which re-derives mode/permissions itself.

    Returns:
        Response text if successful, None if failed
    """
    headers = {"Authorization": f"Bearer {ha_token}"}

    if command_type == "turn_on":
        device = params["device"]
        entity_id = _resolve_device_to_entity(device)
        if entity_id and not entity_id.startswith("light."):
            logger.info(
                "simple_command_fast_path_refused",
                command="turn_on", device=device, entity_id=entity_id,
                reason="not_light_domain",
            )
            return None
        if entity_id and not await fast_path_allowed():
            logger.info(
                "simple_command_fast_path_refused",
                command="turn_on", device=device, entity_id=entity_id,
                reason="mode_service_not_owner",
            )
            return None
        if entity_id:
            try:
                if ha_client:
                    resp = await ha_client.post(
                        f"{ha_url}/api/services/homeassistant/turn_on",
                        headers=headers,
                        json={"entity_id": entity_id}
                    )
                else:
                    async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                        resp = await client.post(
                            f"{ha_url}/api/services/homeassistant/turn_on",
                            headers=headers,
                            json={"entity_id": entity_id}
                        )
                if resp.status_code >= 400:
                    logger.warning(
                        "simple_command_ha_call_failed",
                        command="turn_on", device=device, entity_id=entity_id,
                        status_code=resp.status_code,
                    )
                    return None
                logger.info("simple_command_executed", command="turn_on", device=device, entity_id=entity_id)
                return f"I've turned on the {device}."
            except Exception as e:
                logger.warning(
                    "simple_command_ha_call_error",
                    command="turn_on", device=device, entity_id=entity_id,
                    error_type=type(e).__name__,
                )
                return None
        return None

    elif command_type == "turn_off":
        device = params["device"]
        entity_id = _resolve_device_to_entity(device)
        if entity_id and not entity_id.startswith("light."):
            logger.info(
                "simple_command_fast_path_refused",
                command="turn_off", device=device, entity_id=entity_id,
                reason="not_light_domain",
            )
            return None
        if entity_id and not await fast_path_allowed():
            logger.info(
                "simple_command_fast_path_refused",
                command="turn_off", device=device, entity_id=entity_id,
                reason="mode_service_not_owner",
            )
            return None
        if entity_id:
            try:
                if ha_client:
                    resp = await ha_client.post(
                        f"{ha_url}/api/services/homeassistant/turn_off",
                        headers=headers,
                        json={"entity_id": entity_id}
                    )
                else:
                    async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                        resp = await client.post(
                            f"{ha_url}/api/services/homeassistant/turn_off",
                            headers=headers,
                            json={"entity_id": entity_id}
                        )
                if resp.status_code >= 400:
                    logger.warning(
                        "simple_command_ha_call_failed",
                        command="turn_off", device=device, entity_id=entity_id,
                        status_code=resp.status_code,
                    )
                    return None
                logger.info("simple_command_executed", command="turn_off", device=device, entity_id=entity_id)
                return f"I've turned off the {device}."
            except Exception as e:
                logger.warning(
                    "simple_command_ha_call_error",
                    command="turn_off", device=device, entity_id=entity_id,
                    error_type=type(e).__name__,
                )
                return None
        return None

    elif command_type == "time":
        now = datetime.now()
        hour = now.strftime('%I').lstrip('0')  # Remove leading zero from hour
        minute = now.minute
        if minute == 0:
            time_str = f"{hour} o'clock"
        elif minute < 10:
            time_str = f"{hour} oh {minute}"  # "12 oh 3" not "12 zero 3"
        else:
            time_str = f"{hour} {minute}"
        period = "in the morning" if now.hour < 12 else "in the afternoon" if now.hour < 18 else "in the evening"
        return f"It's {time_str} {period}."

    elif command_type == "date":
        now = datetime.now()
        return f"Today is {now.strftime('%A, %B %d, %Y')}."

    elif command_type == "greeting":
        greeting_type = params.get("type", "")
        if "morning" in greeting_type:
            return "Good morning! How can I help you today?"
        elif "night" in greeting_type:
            return "Good night! Sleep well."
        elif "hello" in greeting_type:
            return "Hello! How can I help you?"

    elif command_type == "thank_you":
        return "You're welcome! Is there anything else I can help with?"

    return None


def _resolve_device_to_entity(device_name: str) -> Optional[str]:
    """Resolve friendly device name to HA entity_id."""
    device_lower = device_name.lower().strip()

    # Check direct mapping first
    if device_lower in DEVICE_MAPPINGS:
        return DEVICE_MAPPINGS[device_lower]

    # Try to construct entity_id from device name
    # e.g., "kitchen lights" -> "light.kitchen"
    normalized = device_lower.replace(" ", "_").replace("lights", "").replace("light", "").strip("_")
    if normalized:
        return f"light.{normalized}"

    return None

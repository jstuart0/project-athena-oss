"""mode_permission — extracted from orchestrator.main during Phase 3.1 (ATHENA-10).

Byte-identical move of 6 mode/permission helpers:
  - get_current_mode         (Pattern 1 — uses _runtime.get_mode_client at call time)
  - detect_owner_mode_command (Pattern 1 PURE)
  - extract_pin_from_query   (Pattern 1 PURE)
  - activate_owner_override  (Pattern 1)
  - check_intent_permission  (Pattern 1)
  - check_entity_permission  (Pattern 2 — accepts permissions dict)

See thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md Phase 3.1.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

import structlog

from orchestrator.nodes._runtime import get_mode_client
from orchestrator.state import IntentCategory

logger = structlog.get_logger(__name__)


class _ModeClientProxy:
    """Forward attribute access to the runtime mode client at call time.

    get_current_mode and activate_owner_override reference ``mode_client`` as a
    bare module global.  The actual client is registered in _runtime by
    main.py's lifespan, so we cannot bind it at import time.  This proxy
    resolves the current value on every attribute lookup, keeping the function
    bodies byte-identical.
    """

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_mode_client(), name)


mode_client = _ModeClientProxy()


# Patterns for detecting owner mode commands
OWNER_MODE_PATTERNS = [
    r"\b(switch|change|enable|activate)\s+(to\s+)?owner\s*mode\b",
    r"\bowner\s*mode\b.*\bpin\b",
    r"\bpin\b.*\bowner\s*mode\b",
    r"\bi'?m\s+(the\s+)?owner\b",
    r"\bexit\s+guest\s*mode\b",
    r"\bdeactivate\s+guest\s*mode\b",
    r"\bowner\s+override\b",
]


async def get_current_mode() -> Dict[str, Any]:
    """
    Get current mode from mode service (Phase 2: Guest Mode).

    Fetches mode (guest vs owner) and permission settings from the mode service.
    Falls back to owner mode if service unavailable (safe default).

    Returns:
        Dict with mode, permissions, and metadata
    """
    try:
        response = await mode_client.get("/mode")
        response.raise_for_status()
        mode_data = response.json()

        # Get permissions for current mode
        perms_response = await mode_client.get("/mode/permissions")
        perms_response.raise_for_status()
        permissions = perms_response.json()

        logger.info(
            "mode_fetched",
            mode=mode_data.get("mode", "owner"),
            override_active=mode_data.get("override_active", False)
        )

        return {
            "mode": mode_data.get("mode", "owner"),
            "permissions": permissions,
            "override_active": mode_data.get("override_active", False),
            "reason": mode_data.get("reason", "Unknown")
        }
    except Exception as e:
        logger.warning(f"Failed to get mode from mode service: {e}")
        # Default to owner mode on error (safe default)
        return {
            "mode": "owner",
            "permissions": {
                "mode": "owner",
                "allowed_intents": [],
                "restricted_entities": [],
                "allowed_domains": [],
                "max_queries_per_minute": 100
            },
            "override_active": False,
            "reason": "Mode service unavailable"
        }


# ============================================================================
# Phase 4: Voice PIN Override Detection and Handling
# ============================================================================

def detect_owner_mode_command(query: str) -> bool:
    """
    Detect if the query is an owner mode command (Phase 4: Voice PIN Override).

    Args:
        query: User query string

    Returns:
        True if query appears to be an owner mode command
    """
    query_lower = query.lower()
    for pattern in OWNER_MODE_PATTERNS:
        if re.search(pattern, query_lower):
            return True
    return False


def extract_pin_from_query(query: str) -> Optional[str]:
    """
    Extract a 6-digit PIN from a query (Phase 4: Voice PIN Override).

    Handles various spoken formats:
    - "pin 123456"
    - "pin one two three four five six"
    - "code 123456"
    - "123456"

    Args:
        query: User query string

    Returns:
        6-digit PIN string or None if not found
    """
    # Word to digit mapping for spoken numbers
    word_to_digit = {
        "zero": "0", "oh": "0", "o": "0",
        "one": "1", "won": "1",
        "two": "2", "to": "2", "too": "2",
        "three": "3", "tree": "3",
        "four": "4", "for": "4", "fore": "4",
        "five": "5",
        "six": "6", "sicks": "6", "sex": "6",
        "seven": "7",
        "eight": "8", "ate": "8",
        "nine": "9", "niner": "9",
    }

    query_lower = query.lower()

    # First try to find numeric digits directly
    # Match 6 consecutive digits
    numeric_match = re.search(r'\b(\d{6})\b', query_lower)
    if numeric_match:
        return numeric_match.group(1)

    # Match 6 digits with spaces between them (e.g., "1 2 3 4 5 6")
    spaced_match = re.search(r'\b(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\b', query_lower)
    if spaced_match:
        return ''.join(spaced_match.groups())

    # Try to find spoken digits after "pin" or "code"
    pin_context = re.search(r'(?:pin|code)\s+(.+)', query_lower)
    if pin_context:
        digit_portion = pin_context.group(1)
        digits = []

        # Split by spaces and convert words to digits
        words = digit_portion.split()
        for word in words:
            # Clean punctuation
            word_clean = re.sub(r'[^\w]', '', word)
            if word_clean.isdigit():
                digits.append(word_clean)
            elif word_clean in word_to_digit:
                digits.append(word_to_digit[word_clean])

            # Stop if we have 6 digits
            if len(digits) >= 6:
                break

        if len(digits) == 6:
            return ''.join(digits)

    return None


async def activate_owner_override(
    pin: Optional[str],
    voice_device_id: Optional[str] = None,
    timeout_minutes: Optional[int] = None
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Activate owner mode override via mode service (Phase 4: Voice PIN Override).

    Args:
        pin: 6-digit PIN or None
        voice_device_id: Optional device identifier
        timeout_minutes: Override duration

    Returns:
        Tuple of (success, message, response_data)
    """
    try:
        request_data = {
            "mode": "owner",
            "voice_pin": pin,
            "timeout_minutes": timeout_minutes,
            "voice_device_id": voice_device_id
        }

        response = await mode_client.post("/mode/override", json=request_data)

        if response.status_code == 200:
            data = response.json()
            logger.info(
                "owner_override_activated",
                expires_at=data.get("expires_at"),
                device=voice_device_id
            )
            return True, data.get("message", "Owner mode activated."), data

        elif response.status_code == 401:
            # PIN required but not provided
            logger.info("owner_override_pin_required", device=voice_device_id)
            return False, "Please provide your 6-digit owner PIN. Say 'owner mode' followed by your PIN.", None

        elif response.status_code == 403:
            # Invalid PIN
            logger.warning("owner_override_pin_invalid", device=voice_device_id)
            return False, "Invalid PIN. Access denied.", None

        elif response.status_code == 400:
            # Invalid PIN format
            detail = response.json().get("detail", "Invalid PIN format")
            logger.warning("owner_override_invalid_format", detail=detail, device=voice_device_id)
            return False, f"{detail}. Please provide a 6-digit PIN.", None

        else:
            logger.error(
                "owner_override_unexpected_error",
                status_code=response.status_code,
                device=voice_device_id
            )
            return False, "Unable to process owner mode request. Please try again.", None

    except Exception as e:
        logger.error(f"owner_override_failed: {e}")
        return False, "Mode service unavailable. Owner mode request could not be processed.", None


def check_intent_permission(intent: IntentCategory, permissions: Dict[str, Any]) -> bool:
    """
    Check if intent is allowed based on current permissions (Phase 2: Guest Mode).

    Uses a deny-list approach: intents in restricted_intents are blocked,
    everything else is allowed (unless allowed_intents is populated).

    Args:
        intent: Intent category
        permissions: Permissions from mode service

    Returns:
        True if allowed, False otherwise
    """
    mode = permissions.get("mode", "owner")

    # Owner mode: everything allowed
    if mode == "owner":
        return True

    intent_value = intent.value.lower()

    # Primary check: restricted_intents (deny list)
    restricted_intents = permissions.get("restricted_intents", [])
    if restricted_intents:
        is_restricted = intent_value in [i.lower() for i in restricted_intents]
        if is_restricted:
            logger.info(
                "intent_blocked_restricted",
                intent=intent_value,
                mode=mode,
                restricted_intents=restricted_intents
            )
            return False

    # Secondary check: allowed_intents (allow list) if populated
    allowed_intents = permissions.get("allowed_intents", [])
    if allowed_intents:
        is_allowed = intent_value in [i.lower() for i in allowed_intents]
        logger.info(
            "intent_permission_check",
            intent=intent_value,
            mode=mode,
            allowed=is_allowed,
            allowed_intents=allowed_intents
        )
        return is_allowed

    # No allow list specified - allow by default (only restricted_intents blocked)
    logger.info(
        "intent_allowed_default",
        intent=intent_value,
        mode=mode
    )
    return True


def check_entity_permission(entity_id: str, permissions: Dict[str, Any]) -> bool:
    """
    Check if entity access is allowed based on current permissions (Phase 2: Guest Mode).

    Uses regex patterns for restricted_entities (e.g., ".*tesla.*", ".*vehicle.*").
    Falls back to domain-based allow list if no match.

    Args:
        entity_id: Home Assistant entity ID (e.g., "light.bedroom")
        permissions: Permissions from mode service

    Returns:
        True if allowed, False otherwise
    """
    import re

    mode = permissions.get("mode", "owner")

    # Owner mode: everything allowed
    if mode == "owner":
        return True

    # Guest mode: check restrictions
    restricted_entities = permissions.get("restricted_entities", [])
    allowed_domains = permissions.get("allowed_domains", [])

    # Check if entity matches restricted pattern (supports regex)
    for pattern in restricted_entities:
        try:
            if re.match(pattern, entity_id, re.IGNORECASE):
                logger.info(
                    "entity_blocked_by_regex",
                    entity_id=entity_id,
                    pattern=pattern,
                    mode=mode
                )
                return False
        except re.error:
            # Invalid regex - try simple wildcard match as fallback
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                if entity_id.startswith(prefix):
                    logger.info(
                        "entity_blocked_by_wildcard",
                        entity_id=entity_id,
                        pattern=pattern,
                        mode=mode
                    )
                    return False
            elif pattern == entity_id:
                logger.info(
                    "entity_blocked_exact",
                    entity_id=entity_id,
                    mode=mode
                )
                return False

    # Check if entity domain is allowed
    entity_domain = entity_id.split(".")[0] if "." in entity_id else entity_id
    if allowed_domains and entity_domain not in allowed_domains:
        logger.info(
            "entity_blocked_domain",
            entity_id=entity_id,
            domain=entity_domain,
            allowed_domains=allowed_domains,
            mode=mode
        )
        return False

    logger.info(
        "entity_allowed",
        entity_id=entity_id,
        mode=mode
    )
    return True

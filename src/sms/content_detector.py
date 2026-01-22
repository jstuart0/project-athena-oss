"""
Content Detection for SMS-worthy information.

Detects content in responses that should be offered as SMS:
- WiFi credentials
- Door codes
- Addresses
- URLs/links
- Reservation codes
- Lists of items
"""

import re
from dataclasses import dataclass
from typing import Tuple, List, Dict, Set


@dataclass
class DetectedContent:
    """Represents detected content that may warrant SMS."""
    content_type: str
    value: str
    start: int
    end: int
    confidence: float
    description: str


# Pattern configuration for content detection
CONTENT_PATTERNS: Dict[str, Dict] = {
    "wifi_password": {
        "pattern": r"(?:wifi|wi-fi|password|network|ssid)[:\s]+['\"]?([^\s'\"]{4,})['\"]?",
        "priority": 10,
        "always_offer": True,
        "description": "WiFi credentials",
    },
    "door_code": {
        "pattern": r"(?:code|pin|keypad|lock|entry|door)[:\s]*[#]?(\d{4,8})",
        "priority": 10,
        "always_offer": True,
        "description": "Door/entry code",
    },
    "address": {
        "pattern": r"(\d+\s+[\w\s]+(?:street|st|avenue|ave|road|rd|drive|dr|boulevard|blvd|lane|ln|way|court|ct|place|pl)[\w\s,]*(?:\d{5}(?:-\d{4})?)?)",
        "priority": 8,
        "always_offer": True,
        "description": "Address",
    },
    "phone_number": {
        "pattern": r"(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})",
        "priority": 6,
        "always_offer": False,
        "description": "Phone number",
    },
    "url": {
        "pattern": r"(https?://[^\s<>\"']+)",
        "priority": 7,
        "always_offer": True,
        "description": "Web link",
    },
    "email": {
        "pattern": r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
        "priority": 5,
        "always_offer": False,
        "description": "Email address",
    },
    "time": {
        "pattern": r"(\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)?|\d{1,2}\s*(?:am|pm|AM|PM))",
        "priority": 4,
        "always_offer": False,
        "description": "Time",
    },
    "reservation_code": {
        "pattern": r"(?:confirmation|reservation|booking|code)[:\s#]*([A-Z0-9]{6,12})",
        "priority": 9,
        "always_offer": True,
        "description": "Reservation code",
    },
}


def detect_textable_content(response: str) -> Tuple[bool, List[DetectedContent], str]:
    """
    Analyze response for content that should be offered as SMS.

    Args:
        response: The response text to analyze

    Returns:
        Tuple of:
        - should_offer: Boolean indicating if SMS should be offered
        - detected: List of detected content items
        - reason: Human-readable reason for offering
    """
    detected: List[DetectedContent] = []

    for content_type, config in CONTENT_PATTERNS.items():
        pattern = config["pattern"]
        matches = re.finditer(pattern, response, re.IGNORECASE | re.MULTILINE)

        for match in matches:
            value = match.group(1) if match.lastindex else match.group(0)
            detected.append(DetectedContent(
                content_type=content_type,
                value=value,
                start=match.start(),
                end=match.end(),
                confidence=config["priority"] / 10.0,
                description=config["description"],
            ))

    if not detected:
        return False, [], ""

    # Sort by priority (higher first)
    detected.sort(key=lambda x: -x.confidence)

    # Check if we should offer SMS
    always_offer_types: Set[str] = {
        ct for ct, cfg in CONTENT_PATTERNS.items()
        if cfg.get("always_offer")
    }
    high_priority_detected = [
        d for d in detected
        if d.content_type in always_offer_types
    ]

    # Offer if we have high-priority content OR 3+ items of any type
    should_offer = len(high_priority_detected) >= 1 or len(detected) >= 3

    # Build reason string
    if should_offer:
        types_found = list(set(d.content_type for d in detected[:3]))
        type_names = [CONTENT_PATTERNS[t]["description"] for t in types_found]
        reason = f"Contains {', '.join(type_names)}"
    else:
        reason = ""

    return should_offer, detected, reason


def extract_sms_content(response: str, detected: List[DetectedContent]) -> str:
    """
    Extract and format detected content for SMS delivery.

    Args:
        response: Original response text
        detected: List of detected content items

    Returns:
        Formatted SMS-friendly content string
    """
    # Group by type
    by_type: Dict[str, List[str]] = {}
    for d in detected:
        if d.content_type not in by_type:
            by_type[d.content_type] = []
        by_type[d.content_type].append(d.value)

    # Type ordering and icons for SMS formatting
    type_order = [
        "wifi_password", "door_code", "address", "reservation_code",
        "url", "phone_number", "email", "time"
    ]
    type_icons = {
        "wifi_password": "WiFi",
        "door_code": "Code",
        "address": "Address",
        "reservation_code": "Confirmation",
        "url": "Link",
        "phone_number": "Phone",
        "email": "Email",
        "time": "Time",
    }

    # Build formatted output
    lines = []
    for content_type in type_order:
        if content_type in by_type:
            label = type_icons.get(content_type, content_type.replace("_", " ").title())
            values = by_type[content_type]

            if len(values) == 1:
                lines.append(f"{label}: {values[0]}")
            else:
                lines.append(f"{label}:")
                for v in values:
                    lines.append(f"  - {v}")

    return "\n".join(lines)


def get_primary_content_type(detected: List[DetectedContent]) -> str:
    """
    Get the primary (highest priority) content type from detected items.

    Args:
        detected: List of detected content items

    Returns:
        Primary content type string or 'custom'
    """
    if not detected:
        return "custom"

    # Already sorted by priority in detect_textable_content
    return detected[0].content_type


def summarize_content(content: str, max_length: int = 50) -> str:
    """
    Create a brief summary of SMS content for logging/display.

    Args:
        content: Full SMS content
        max_length: Maximum summary length

    Returns:
        Truncated summary string
    """
    # Remove newlines for compact summary
    summary = content.replace("\n", " ").strip()

    if len(summary) <= max_length:
        return summary

    return summary[:max_length - 3] + "..."

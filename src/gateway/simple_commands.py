"""
Simple command detection and execution for HA voice fast-path.

Detects patterns like:
- "What time is it?" / "What's the date?"
- "Good morning/night", "hello", "thanks"

These are replies only: nothing here talks to Home Assistant. Device control
(turn on/off, set) always goes to the orchestrator, which decides the room,
authorizes every write and applies the fan-out gate.
"""

import re
from typing import Optional, Tuple
import structlog

from shared.local_time import local_now

logger = structlog.get_logger("gateway.simple_commands")

# Simple command patterns
PATTERNS = {
    "time": re.compile(r"what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it", re.IGNORECASE),
    "date": re.compile(r"what(?:'s|\s+is)\s+(?:the\s+)?(?:today'?s?\s+)?date|what\s+day\s+is\s+(?:it|today)", re.IGNORECASE),
    "greeting_morning": re.compile(r"good\s+morning", re.IGNORECASE),
    "greeting_night": re.compile(r"good\s+night", re.IGNORECASE),
    "greeting_hello": re.compile(r"^(?:hello|hi|hey)(?:\s+(?:there|jarvis|athena))?[.!?]?$", re.IGNORECASE),
    "thank_you": re.compile(r"^(?:thanks?(?:\s+you)?|thank\s+you(?:\s+(?:very\s+much|so\s+much))?)[.!?]?$", re.IGNORECASE),
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
        match = pattern.match(query)
        if match:
            if pattern_name in ["time", "date"]:
                return (pattern_name, {})
            elif pattern_name.startswith("greeting"):
                return ("greeting", {"type": pattern_name})
            elif pattern_name == "thank_you":
                return ("thank_you", {})

    return None


async def execute_simple_command(command_type: str, params: dict) -> Optional[str]:
    """
    Answer a simple command locally. Returns the reply text, or None for a
    command type this module doesn't answer.
    """
    if command_type == "time":
        now = local_now()
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
        now = local_now()
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

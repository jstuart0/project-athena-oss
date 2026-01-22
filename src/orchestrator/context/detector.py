"""
Context Reference Detection

Analyzes queries to detect if they reference previous conversation context.
This enables follow-up queries like "do that again", "turn them off", "what about tomorrow?".
"""

from typing import Dict, Any


# Context reference detection patterns
CONTEXT_REF_PATTERNS = {
    # Action references - "do that", "repeat", "again"
    "action_refs": ["do that", "same thing", "do it", "repeat", "again", "do the same", "that too", "also"],
    # Pronouns referring to previous entities (including personal pronouns for person references)
    "pronouns": ["them", "it", "those", "that one", "the same one", "there",
                 "he", "she", "him", "her", "his", "they", "their"],
    # Modifiers that imply previous context
    "modifiers": ["brighter", "dimmer", "louder", "quieter", "warmer", "cooler", "different color",
                  "more", "less", "instead", "but", "rather"],
    # Follow-up phrases
    "follow_ups": ["what about", "how about", "and", "also", "too", "as well", "or maybe"],
    # Temporal follow-ups (for weather, events)
    "temporal": ["tomorrow", "next week", "this weekend", "later", "tonight", "yesterday"],
    # Inquiry about previous actions - "which lights did you turn on?", "what did you do?"
    # NOTE: "what happened" and "what was the error" are handled separately as meta_inquiry
    "inquiry": ["which", "what did you", "did you", "did it work",
                "was it", "were they", "are they", "is it"],
    # Meta-inquiries about system state/errors - these should NOT continue previous context
    # because they're asking about the system itself, not about the previous topic
    "meta_inquiry": [
        "what happened", "what was the error", "what went wrong", "why did it fail",
        "what's the problem", "what is the problem", "why didn't it work",
        "what error", "got an error", "there was an error", "something went wrong",
        "why did that fail", "what was that error", "what's wrong", "what is wrong",
        "having trouble", "not working", "didn't work", "doesn't work",
        # Round 17: Additional meta-inquiry patterns
        "what was the issue", "what is the issue", "what's the issue",
        "why couldn't", "why can't", "what failed", "why failed"
    ],
    # Short continuation responses - answers to Athena's questions
    "continuations": [
        "yes", "no", "ok", "okay", "sure", "nope", "yeah", "yep", "yup",
        "nah", "please", "thanks", "go ahead", "sounds good", "that works",
        "perfect", "fine", "great", "awesome", "cool", "alright", "right",
        "correct", "exactly", "definitely", "absolutely", "of course",
        "not really", "i guess", "maybe", "perhaps", "probably",
        "any", "anything", "whatever", "surprise me", "you choose",
        "i don't care", "doesn't matter", "no preference", "no preferences"
    ],
    # Incomplete commands - missing object, implies previous context
    # e.g., "set to level 2" (set WHAT?), "change to blue" (change WHAT?)
    "incomplete_commands": [
        "set to", "change to", "switch to", "turn to",
        "set at", "put at", "put to",
        "make to", "adjust to",
        "level ", "percent", "%",
        "lower", "higher", "up", "down",  # "turn up", "turn down" without object
    ],
    # Conversation breakers - phrases that indicate user is making a conversational
    # statement that should NOT continue the previous intent. These break context.
    "conversation_breakers": [
        # Giving up / abandoning task
        "forget it", "forget about it", "never mind", "nevermind", "i'll do it myself",
        "do it myself", "i got it", "i've got it", "i can do it", "just forget it",
        "don't worry about it", "don't bother", "skip it", "leave it",
        # Frustration / complaints
        "useless", "terrible", "worthless", "stupid", "dumb", "sucks",
        "doesn't work", "not working", "broken", "you're wrong",
        # Apologies
        "i'm sorry", "im sorry", "my bad", "i apologize", "sorry about that",
        "that was mean", "that was rude", "i didn't mean",
        # Gratitude (standalone, not task-related)
        "thanks for being patient", "thank you for being patient",
        "appreciate your patience", "thanks for your help", "thank you for your help",
        "thanks for trying", "thank you for trying",
        # Farewells / goodbyes
        "goodbye", "good bye", "bye", "goodnight", "good night", "nighty night",
        "peace", "peace out", "later", "see ya", "see you", "take care",
        "catch you later", "i'm out", "im out", "gotta go", "got to go",
        "lol ok peace", "lol okay peace", "ok peace", "okay peace",
        "k bye", "ok bye", "okay bye", "alright bye", "k thx", "k thanks",
        # Emotional responses that are conversational, not commands
        "bad day", "rough day", "tough day", "stressed out", "frustrated",
        "i'm tired", "im tired", "exhausted",
    ],
}

# Room/location indicators for control context
ROOM_INDICATORS = [
    "in the", "upstairs", "downstairs", "hallway", "bedroom", "kitchen",
    "living room", "office", "bathroom", "basement", "garage", "dining",
    "master", "guest", "front", "back", "outside"
]

# Strong intent indicators - keywords that clearly indicate a specific intent
# and should NOT be handled as context continuation even for short queries.
# If a query contains these keywords, it should be classified fresh, not
# routed to the previous intent.
STRONG_INTENT_INDICATORS = {
    "control": [
        # Occupancy/presence queries (current)
        "anyone home", "anybody home", "someone home", "who's home", "who is home",
        "is anyone", "is anybody", "is someone", "anyone there", "anybody there",
        # Occupancy/presence queries (past tense / temporal)
        "someone was home", "anyone was home", "anybody was home",
        "last time someone", "last time anyone", "last time somebody",
        "when was someone", "when was anyone", "when was somebody",
        "when was the last", "last motion", "last movement", "last activity",
        "recent motion", "recent activity", "who was home", "who was here",
        # Motion/sensor queries
        "motion", "occupancy", "movement", "sensor", "sensors",
        # Device control
        "turn on", "turn off", "lights", "light", "switch", "fan", "thermostat",
        "temperature inside", "set the", "dim the", "brighten",
        # Home state queries
        "doors", "windows", "locked", "unlocked", "garage", "alarm",
        # Music/audio control - these should override any previous context
        "play music", "play some music", "play the music", "stop music", "stop the music",
        "pause music", "pause the music", "resume music",
        "play hip hop", "play hip-hop", "play jazz", "play rock", "play classical",
        "play country", "play pop", "play r&b", "play rnb", "play blues",
        "play reggae", "play soul", "play funk", "play disco", "play electronic",
        "play ambient", "play chill", "play relaxing", "play upbeat", "play workout",
        "play something", "play songs", "play a song", "put on music", "put on some music",
        "music in the", "music in my", "start playing", "start music",
        # Audio control
        "volume up", "volume down", "turn up the volume", "turn down the volume",
        "louder", "quieter", "mute", "unmute",
        # Automation/scheduling queries
        "every hour", "every minute", "every 30 minutes", "every 15 minutes",
        "at sunset", "at sunrise", "when motion", "when the door",
        "when button", "when doorbell", "create automation", "create a routine",
        "schedule", "when i arrive", "when i leave", "when i get home",
    ],
    "dining": [
        "restaurant", "restaurants", "food", "eat", "eating", "dinner",
        "lunch", "breakfast", "brunch", "dining", "cuisine", "meal",
        "takeout", "delivery", "reservation", "reservations", "hungry",
        "place to eat", "where to eat", "good food", "best food",
        "recommend", "recommendations", "recommendation", "suggest",
        "happy hour", "specials", "menu", "bar", "pub", "cafe", "coffee shop",
        "pizza", "pizzeria", "sushi", "thai", "chinese", "mexican", "italian", "itallian",
        "burger", "burgers", "tacos", "taco", "wings", "bbq", "barbecue", "steak", "steakhouse",
        "ramen", "pho", "indian", "korean", "japanese", "vietnamese", "greek", "mediterranean",
        # Places/POI searches (gas stations, EV chargers, rest stops, etc.)
        "supercharger", "superchargers", "charging station", "ev charger", "ev charging",
        "tesla charger", "chargepoint", "electrify america", "gas station", "gas stations",
        "rest stop", "rest area", "truck stop", "service plaza", "convenience store",
        "bathroom", "restroom", "atm", "pharmacy", "grocery", "groceries",
        "along the way", "on the way", "nearby", "near me", "closest", "nearest",
    ],
    "weather": [
        "weather", "forecast", "temperature", "rain", "raining", "sunny",
        "cloudy", "snow", "snowing", "storm", "humidity", "wind",
    ],
    "flights": [
        "flight", "flights", "plane", "airplane", "airline", "booking",
        "ticket", "tickets", "departure", "arrival", "layover",
    ],
    "airports": [
        "airport", "airports", "terminal", "gate", "tsa", "baggage",
    ],
    "sports": [
        "score", "scores", "game", "games", "match", "team", "teams",
        "nfl", "nba", "mlb", "nhl", "football", "basketball", "baseball",
        "hockey", "soccer", "standings", "playoffs", "championship",
    ],
    "stocks": [
        "stock", "stocks", "market", "trading", "price", "share", "shares",
        "portfolio", "investment", "nasdaq", "dow", "s&p",
    ],
    "news": [
        "news", "headlines", "article", "articles", "breaking", "latest news",
    ],
    "events": [
        "event", "events", "concert", "concerts", "show", "shows",
        "festival", "convention", "conference", "exhibition",
    ],
    "streaming": [
        "movie", "movies", "film", "films", "tv show", "series", "watch",
        "watching", "streaming", "netflix", "hulu", "disney",
    ],
    "recipes": [
        "recipe", "recipes", "cook", "cooking", "bake", "baking",
        "ingredient", "ingredients", "how to make", "how to cook",
    ],
}


def detect_strong_intent(query: str, prev_intent: str = None) -> Dict[str, Any]:
    """
    Detect if a query contains strong intent indicators that should override
    context continuation. This prevents "restaurant recommendations" from being
    routed to weather just because the previous query was about weather.

    Args:
        query: The user's query text
        prev_intent: The previous intent (optional) - if the strong indicator
                    matches the previous intent, context continuation is allowed

    Returns:
        Dict containing:
        - has_strong_intent: Whether a strong intent indicator was found
        - detected_intent: The intent category indicated (e.g., "dining")
        - matching_keywords: List of keywords that matched
        - should_override_context: True if this should NOT continue prev context
    """
    query_lower = query.lower().strip()
    result = {
        "has_strong_intent": False,
        "detected_intent": None,
        "matching_keywords": [],
        "should_override_context": False,
    }

    # Round 17 FIX: Thermostat control patterns should NOT trigger weather override
    # "set temperature to 70" is thermostat control, NOT a weather query
    thermostat_control_patterns = [
        "set temperature", "set the temperature", "set temp",
        "change temperature", "change the temperature", "change temp",
        "raise temperature", "lower temperature", "raise the temp", "lower the temp",
        "turn up the heat", "turn down the heat", "turn up the ac", "turn down the ac",
        "make it warmer", "make it cooler", "make it hotter", "make it colder",
        "degrees warmer", "degrees cooler", "degrees hotter", "degrees colder",
        "bump up the heat", "bump up the temp", "crank up the heat",
    ]
    is_thermostat_control = any(p in query_lower for p in thermostat_control_patterns)

    # Collect ALL matches first, then pick the best one
    all_matches = {}
    for intent, keywords in STRONG_INTENT_INDICATORS.items():
        # Use word boundary matching to avoid "doors" matching inside "outdoors"
        matching = []
        for kw in keywords:
            # Multi-word keywords can use substring match (e.g., "turn on")
            # Single words need word boundary check to avoid false matches
            if " " in kw:
                if kw in query_lower:
                    matching.append(kw)
            else:
                # Single word - use word boundary matching
                import re
                pattern = r'\b' + re.escape(kw) + r'\b'
                if re.search(pattern, query_lower):
                    matching.append(kw)
        if matching:
            # Round 17 FIX: Skip weather detection if this is a thermostat control command
            if intent == "weather" and is_thermostat_control:
                continue  # Don't let "temperature" trigger weather for thermostat commands
            all_matches[intent] = matching

    if all_matches:
        # Priority intents - these should win when their specific keywords are present
        # even if other generic keywords also match
        priority_intents = ["dining", "weather", "sports", "control"]

        # Choose best match based on:
        # 1. If a priority intent has specific food/weather/sports keywords, prefer it
        # 2. Otherwise, prefer intent with most keyword matches
        # 3. Or prefer priority intents over generic ones like "events"

        best_intent = None
        best_keywords = []

        # First check priority intents for strong specific matches
        for intent in priority_intents:
            if intent in all_matches:
                # Dining with food-specific keywords should win over "show" in events
                if intent == "dining" and any(kw in all_matches[intent] for kw in
                    ["food", "restaurant", "eat", "dinner", "lunch", "breakfast", "cuisine",
                     "pizza", "sushi", "thai", "chinese", "mexican", "italian", "burger",
                     "tacos", "steak", "bbq", "bar", "cafe"]):
                    best_intent = intent
                    best_keywords = all_matches[intent]
                    break

        # If no priority match, pick the one with most keywords
        if not best_intent:
            for intent, keywords in all_matches.items():
                if not best_intent or len(keywords) > len(best_keywords):
                    best_intent = intent
                    best_keywords = keywords

        result["has_strong_intent"] = True
        result["detected_intent"] = best_intent
        result["matching_keywords"] = best_keywords
        # Only override context if the detected intent differs from prev intent
        if prev_intent and prev_intent.lower() != best_intent:
            result["should_override_context"] = True
        elif not prev_intent:
            # No previous intent, so this is a fresh query
            result["should_override_context"] = False

    return result


def detect_context_reference(query: str) -> Dict[str, Any]:
    """
    Analyze a query to detect if it references previous conversation context.
    Returns dict with detected reference types and suggested intent override.

    Args:
        query: The user's query text

    Returns:
        Dict containing:
        - has_context_ref: Whether context reference was detected
        - ref_types: List of detected reference types
        - suggested_intent: Suggested intent based on context
        - has_room_indicator: Whether a room/location was mentioned
        - has_temporal_ref: Whether a temporal reference was found
        - has_modifier: Whether a modifier was found
        - is_short_query: Whether query is 8 words or fewer
        - is_inquiry: Whether query is asking about previous action
        - is_continuation: Whether this is a short response to a previous question
        - is_meta_inquiry: Whether query is asking about system state/errors (should NOT continue context)
        - is_conversation_breaker: Whether query is a conversational response that breaks task context
    """
    query_lower = query.lower().strip()
    result = {
        "has_context_ref": False,
        "ref_types": [],
        "suggested_intent": None,
        "has_room_indicator": False,
        "has_temporal_ref": False,
        "has_modifier": False,
        "is_short_query": len(query_lower.split()) <= 8,
        "is_inquiry": False,
        "is_continuation": False,
        "is_meta_inquiry": False,  # Meta-inquiries about system/errors should NOT continue previous context
        "is_conversation_breaker": False,  # Conversation breakers should NOT continue previous context
    }

    # Check for action references
    if any(ref in query_lower for ref in CONTEXT_REF_PATTERNS["action_refs"]):
        result["has_context_ref"] = True
        result["ref_types"].append("action")

    # Check for pronouns (word-level for single words, substring for multi-word)
    words = query_lower.split()
    # Short pronouns that could match substrings in other words - only word-level match
    short_pronouns = {"he", "she", "him", "her", "his", "it", "they", "them", "their"}
    # Multi-word or longer pronouns - can use substring match
    long_pronouns = [p for p in CONTEXT_REF_PATTERNS["pronouns"] if p not in short_pronouns]

    has_pronoun = (
        any(pron in words for pron in short_pronouns if pron in CONTEXT_REF_PATTERNS["pronouns"]) or
        any(pron in query_lower for pron in long_pronouns)
    )
    if has_pronoun:
        result["has_context_ref"] = True
        result["ref_types"].append("pronoun")

    # Check for modifiers
    if any(mod in query_lower for mod in CONTEXT_REF_PATTERNS["modifiers"]):
        result["has_context_ref"] = True
        result["ref_types"].append("modifier")
        result["has_modifier"] = True

    # Check for follow-up phrases
    if any(fu in query_lower for fu in CONTEXT_REF_PATTERNS["follow_ups"]):
        result["has_context_ref"] = True
        result["ref_types"].append("follow_up")

    # Check for temporal references
    if any(temp in query_lower for temp in CONTEXT_REF_PATTERNS["temporal"]):
        result["has_temporal_ref"] = True
        result["ref_types"].append("temporal")

    # Check for META-inquiry about system state/errors FIRST
    # Meta-inquiries ask about errors, problems, what went wrong - NOT about the previous topic
    # These should NOT trigger context continuation because user wants system info, not topic continuation
    if any(meta in query_lower for meta in CONTEXT_REF_PATTERNS["meta_inquiry"]):
        result["ref_types"].append("meta_inquiry")
        result["is_meta_inquiry"] = True
        # Explicitly do NOT set has_context_ref - this prevents routing to previous intent
        # Return early to avoid matching regular inquiry patterns
    else:
        # Check for regular inquiry about previous actions (only if not meta_inquiry)
        if any(inq in query_lower for inq in CONTEXT_REF_PATTERNS["inquiry"]):
            result["has_context_ref"] = True
            result["ref_types"].append("inquiry")
            result["is_inquiry"] = True

    # Check for continuation responses (short answers to Athena's questions)
    # These are responses like "no", "yes", "sure", "sounds good" that answer
    # a question Athena asked in the previous turn
    words = query_lower.split()
    if result["is_short_query"]:
        # Check if the entire query matches a continuation pattern
        # or if it starts with one (e.g., "no thanks", "yes please")
        for cont in CONTEXT_REF_PATTERNS["continuations"]:
            if query_lower == cont or query_lower.startswith(cont + " ") or query_lower.startswith(cont + ","):
                result["has_context_ref"] = True
                result["ref_types"].append("continuation")
                result["is_continuation"] = True
                break

    # Check for room indicators
    if any(room in query_lower for room in ROOM_INDICATORS):
        result["has_room_indicator"] = True

    # Short queries with just a location/entity often imply context
    # e.g., "the kitchen" after "turn on the office lights"
    if result["is_short_query"] and result["has_room_indicator"]:
        result["has_context_ref"] = True
        result["ref_types"].append("implicit_location")

    # Check for incomplete commands (missing object)
    # e.g., "set to level 2" (set WHAT?), "change to blue" (change WHAT?)
    # These are short commands that need previous context to determine the target
    if result["is_short_query"]:
        if any(cmd in query_lower for cmd in CONTEXT_REF_PATTERNS["incomplete_commands"]):
            result["has_context_ref"] = True
            result["ref_types"].append("incomplete_command")

    # Check for conversation breakers - phrases that should NOT continue previous intent
    # These are emotional/conversational responses like "forget it", "I'm sorry", "thanks"
    # that break out of the current task context
    if any(breaker in query_lower for breaker in CONTEXT_REF_PATTERNS.get("conversation_breakers", [])):
        result["is_conversation_breaker"] = True
        result["ref_types"].append("conversation_breaker")
        # Override has_context_ref - conversation breakers should NOT continue previous context
        result["has_context_ref"] = False
        result["is_continuation"] = False

    return result


# Location correction patterns
# These indicate user wants to change the assumed location
LOCATION_CORRECTION_PATTERNS = [
    # Explicit corrections
    r"\bi(?:'m| am) not in (\w+(?:\s+\w+)?)",  # "I'm not in Baltimore"
    r"\bnot in (\w+(?:\s+\w+)?)\b",  # "not in Baltimore"
    r"\bi(?:'m| am) in (\w+(?:\s+\w+)?)\b",  # "I'm in Northampton"
    r"\bi(?:'m| am) at (\w+(?:\s+\w+)?)\b",  # "I'm at Northampton"
    r"\bi(?:'m| am) near (\w+(?:\s+\w+)?)\b",  # "I'm near Northampton"
    # Location override requests
    r"\buse my (?:current |actual |real )?location\b",
    r"\bmy (?:current |actual |real )?location\b",
    r"\bwhere i am\b",
    r"\bwhere i(?:'m| am)\b",
    # Wrong location corrections
    r"\bwrong (?:location|city|place|area)\b",
    r"\bthat(?:'s| is) wrong\b",
    r"\bthat(?:'s| is) not (?:right|correct|where i am)\b",
    r"\bdifferent (?:location|city|place|area)\b",
    r"\bchange (?:the |my )?location\b",
    r"\bnot (?:there|that location|that city|that area)\b",
]

import re as _re  # Local import to avoid circular dependency

def detect_location_correction(query: str) -> Dict[str, Any]:
    """
    Detect if user is trying to correct/override their location.

    Returns dict with:
    - is_correction: Whether this is a location correction
    - correction_type: Type of correction ('explicit', 'use_current', 'wrong_location')
    - extracted_location: Location mentioned (if any)
    - use_current_location: Whether to use device's current location
    """
    query_lower = query.lower().strip()
    result = {
        "is_correction": False,
        "correction_type": None,
        "extracted_location": None,
        "use_current_location": False,
    }

    # Check for "use my location" type requests
    use_current_patterns = [
        r"\buse my (?:current |actual |real )?location\b",
        r"\bmy (?:current |actual |real )?location\b",
        r"\bwhere i am\b",
        r"\bwhere i(?:'m| am)\b",
    ]
    for pattern in use_current_patterns:
        if _re.search(pattern, query_lower):
            result["is_correction"] = True
            result["correction_type"] = "use_current"
            result["use_current_location"] = True
            return result

    # Check for explicit location mentions
    location_mention_patterns = [
        (r"\bi(?:'m| am) not in (\w+(?:\s+\w+)?)", "not_in"),
        (r"\bi(?:'m| am) in (\w+(?:\s+\w+)?)\b", "in"),
        (r"\bi(?:'m| am) at (\w+(?:\s+\w+)?)\b", "at"),
        (r"\bi(?:'m| am) near (\w+(?:\s+\w+)?)\b", "near"),
        (r"(?:find|search|look) (?:in|around|near) (\w+(?:\s+\w+)?)\b", "search_in"),
    ]
    for pattern, correction_type in location_mention_patterns:
        match = _re.search(pattern, query_lower)
        if match:
            location = match.group(1).strip()
            # Filter out common words that aren't locations
            non_locations = ["the", "a", "an", "my", "your", "this", "that", "here", "there"]
            if location and location not in non_locations:
                result["is_correction"] = True
                result["correction_type"] = correction_type
                result["extracted_location"] = location.title()
                return result

    # Check for "wrong location" type phrases
    wrong_location_patterns = [
        r"\bwrong (?:location|city|place|area)\b",
        r"\bthat(?:'s| is) wrong\b",
        r"\bthat(?:'s| is) not (?:right|correct|where i am)\b",
        r"\bdifferent (?:location|city|place|area)\b",
        r"\bchange (?:the |my )?location\b",
        r"\bnot (?:there|that location|that city|that area)\b",
    ]
    for pattern in wrong_location_patterns:
        if _re.search(pattern, query_lower):
            result["is_correction"] = True
            result["correction_type"] = "wrong_location"
            # User indicated wrong location but didn't specify new one
            # This should trigger clarification or use device location
            result["use_current_location"] = True
            return result

    return result

"""
Semantic Query Caching for Athena Pipeline Latency Optimization

This module provides intent-based caching for RAG queries, significantly reducing
latency for repeated similar queries. Cache keys are based on semantic intent
rather than exact query text.

Expected savings: 1-3 seconds for cached queries (eliminates RAG API calls)
"""

import hashlib
import re
from typing import Optional, Tuple, Dict, Any
from datetime import datetime, timezone

from shared.cache import get_cache_client
import structlog

logger = structlog.get_logger(__name__)


# Category-specific TTLs (in seconds)
# Based on research: weather changes frequently, dining less so, facts rarely
CACHE_TTL_CONFIG = {
    "weather": 300,        # 5 minutes - weather updates frequently
    "dining": 1800,        # 30 minutes - restaurant availability changes slowly
    "news": 900,           # 15 minutes - news updates moderately
    "stocks": 60,          # 1 minute - stock prices change frequently
    "sports": 300,         # 5 minutes - scores during games
    "events": 3600,        # 1 hour - event schedules are stable
    "airports": 300,       # 5 minutes - flight status changes
    "flights": 300,        # 5 minutes - flight status changes
    "recipes": 86400,      # 24 hours - recipes don't change
    "general": 3600,       # 1 hour - general facts
    "streaming": 1800,     # 30 minutes - content availability
    "directions": 300,     # 5 minutes - traffic changes, but route is stable

    # NEVER cache these intents
    "time": 0,             # Always stale
    "smart_home": 0,       # State-changing commands
    "device_control": 0,   # State-changing commands
    "memory": 0,           # Personal/contextual
    "conversation": 0,     # Context-dependent
    "calendar": 0,         # Time-sensitive
}


# Intent patterns that should NEVER be cached
UNCACHEABLE_PATTERNS = [
    r"\bwhat time\b",
    r"\bwhat.{0,10}date\b",
    r"\bcurrent time\b",
    r"\bturn (on|off)\b",
    r"\bset\s+(the\s+)?temperature\b",
    r"\bremember\b",
    r"\bforget\b",
    r"\bschedule\b",
    r"\bremind me\b",
    # Light control commands - state-changing, should always execute fresh
    r"\b(blue|red|green|yellow|orange|purple|pink|cyan|magenta|white)\b",  # Color commands
    r"\b(sunset|sunrise|ocean|christmas|rainbow|forest|fire)\b",  # Ambient color commands (Round 17: added sunrise)
    r"\b(dim|bright|brightness|brighter|dimmer|fade)\b",  # Brightness commands (Round 17: added fade)
    r"\bset\s+(the\s+)?lights?\b",  # Set lights
    r"\bmake\s+(the\s+)?(it|lights?|room)\b",  # Make it/lights/room
    r"\bchange\s+(the\s+)?(lights?|color)\b",  # Change lights/color
    # Context-aware light commands - room from request context
    r"\bmore\s+light\b",  # More light (needs room context)
    r"\bless\s+light\b",  # Less light (needs room context)
    r"\btoo\s+dark\b",  # Too dark (needs room context)
    r"\btoo\s+bright\b",  # Too bright (needs room context)
    r"\bmake\s+it\s+cozy\b",  # Cozy mode (needs room context)
    r"\bbrighten\s+up\b",  # Brighten up (needs room context)
    r"\bin\s+here\b",  # "in here" implies current room
    # Implicit brightness requests - context-dependent
    r"\bcan'?t\s+see\b",  # Can't see (needs room context)
    r"\bcannot\s+see\b",  # Cannot see
    r"\bhard\s+to\s+see\b",  # Hard to see
    # Greetings - context-dependent responses
    r"^good\s+(morning|afternoon|evening|night)$",  # Greetings
    r"^(hello|hi|hey)\b",  # Simple greetings
    # Presence/occupancy queries - state-dependent
    r"\banyone\s+home\b",  # Anyone home
    r"\banybody\s+home\b",  # Anybody home
    r"\bwho.{0,5}home\b",  # Who's home
    r"\bis\s+(anyone|anybody)\b",  # Is anyone/anybody
    r"\boccupancy\b",  # Occupancy queries
    # Music control - state-dependent
    r"^resume$",  # Just "resume" -> resume music
    r"\bresume\s+(the\s+)?music\b",  # Resume music
    r"\bpause\b",  # Pause music
    r"\bnext\s+(song|track)\b",  # Next song
    r"^next$",  # Just "next" -> skip track
    r"^skip$",  # Just "skip" -> skip track
    r"^stop$",  # Just "stop" -> stop music
    r"\bskip\b",  # Skip track
    r"\b(louder|quieter)\b",  # Volume control
    r"\bvolume\s+(up|down)\b",  # Volume up/down
    r"\bturn\s+it\s+(up|down)\b",  # Turn it up/down
    r"\bshuffle\b",  # Shuffle music
    r"\brepeat\b",  # Repeat music
    r"\bloop\b",  # Loop music
    r"^previous$",  # Just "previous" -> previous track
    r"\bprevious\s+(song|track)\b",  # Previous song/track
    # Lock commands - state-changing
    r"\block\b",  # Lock commands
    r"\bunlock\b",  # Unlock commands
    # Whole house control - state-changing
    r"\ball\s+(the\s+)?lights\b",  # All lights
    r"\beverything\s+(on|off)\b",  # Everything on/off
    r"\bturn\s+everything\b",  # Turn everything
    # Indoor temperature queries - route to thermostat, not weather cache
    r"\btemperature\s+(inside|in\s+(the\s+)?house|in\s+here)\b",  # Temperature inside/in house
    r"\b(inside|indoor|indoors)\s+temp(erature)?\b",  # Indoor temp
    r"\b(thermostat|hvac|heat|ac|heating|cooling)\b",  # Thermostat queries
    r"\bmake\s+it\s+(warmer|cooler|hotter|colder)\b",  # Temp adjustment
    # Personal/memory queries - these need user-specific context
    r"\b(my|i|me|mine)\b.*\b(own|have|drive|car|vehicle|tesla)\b",
    r"\b(what|which)\b.*\b(do i|did i|my)\b",
    r"\b(how many|how much)\b.*\b(did i|do i|my)\b",
    r"\b(when did|where did|why did)\b.*\b(i|my)\b",
    r"\babout me\b",
    r"\babout my\b",
    r"\bmy name\b",
    r"\bwho am i\b",
    # Problem-reporting queries - need fresh troubleshooting each time
    r"\bnot (getting|working|turning|heating|cooling)\b",
    r"\b(isn't|isnt|won't|wont|doesn't|doesnt|can't|cant)\s+(work|turn|show|heat|cool|connect)",
    r"\bstopped (working|heating|cooling|responding)\b",
    r"\b(broken|stuck|failed|failing|error)\b",
    r"\bblack screen\b",
    r"\bno (power|signal|response|sound|heat|cold|water)\b",
    r"\bkeeps (turning|shutting|stopping|freezing|crashing)\b",
    r"\bproblem with\b",
    r"\bissue with\b",
    r"\bsomething.{0,10}wrong\b",
    # Round 10 additions - light control phrases
    r"\bhit\s+the\s+lights?\b",  # Hit the lights
    r"\blights?\s+please\b",  # Lights please
    r"\bjust\s+a\s+little\s+light\b",  # Just a little light
    r"\bsome\s+light\b",  # Some light (over here, please)
    r"\bgive\s+me\s+some\s+light\b",  # Give me some light
    r"\bnot\s+so\s+bright\b",  # Not so bright
    r"\bcut\s+the\s+lights?\b",  # Cut the lights
    r"\bno\s+more\s+lights?\b",  # No more lights
    r"\bbring\s+up\s+the\s+lights?\b",  # Bring up the lights
    r"\btoo\s+much\s+light\b",  # Too much light
    # Volume/now playing additions
    r"\bcan'?t\s+hear\b",  # Can't/cant hear it
    r"\bturn\s+up\s+the\s+music\b",  # Turn up the music
    r"\bvolume\s+way\s+up\b",  # Volume way up
    r"\bsong\s+(called|name)\b",  # Song called/name (now playing query)
    r"\bwhats?\s+(that|this|the)\s+song\b",  # Whats that/this/the song
    # Exclusion patterns
    r"\bleave\s+.+\s+on\s+.+\s+turn\s+off\b",  # Leave X on but turn off the rest
    # Round 11 additions
    r"\blight\s+me\s+up\b",  # Light me up
    r"\bthrow\s+on\s+.+lights?\b",  # Throw on some/the lights
    r"\bdarken\s+it\s+up\b",  # Darken it up
    r"\btone\s+down\b",  # Tone down the lights
    r"\bflip\s+.+lights?\b",  # Flip the lights
    r"\bshut\s+it\s+off\b",  # Shut it off
    r"\bshut\s+off\b",  # Shut off
    r"\bplay\s+the\s+next\s+one\b",  # Play the next one
    r"\bgo\s+back\s+one\s+song\b",  # Go back one song
    r"\block\s+up\b",  # Lock up
    r"\ball\s+lights?\s+.+\s+half\b",  # All lights at half
    r"\bforget\s+the\s+lights?\b",  # Forget the lights (turn off, not memory)
    r"\beasy\s+on\s+my\s+eyes\b",  # Take it easy on my eyes (dimmer)
    r"\btake\s+it\s+easy\s+on\s+my\s+eyes\b",  # Full phrase
    # Round 12 additions
    r"\boff\s+with\s+the\s+lights?\b",  # Off with the lights
    r"\bflip\s+em\s+on\b",  # Flip em on
    r"\bflip\s+them\s+on\b",  # Flip them on
    r"\bevery\s+light\s+(on|off)\b",  # Every light on/off
    r"\bbring\s+(the\s+)?lights?\s+down\b",  # Bring the lights down
    r"\bbring\s+down\s+the\s+lights?\b",  # Bring down the lights
    r"\bput\s+on\s+the\s+next\b",  # Put on the next track
    r"\bpump\s+up\s+the\s+jam\b",  # Pump up the jam
    r"\bmore\s+volume\b",  # Give it more volume
    r"\bvibe\b",  # Color vibe
    r"\bdrop\s+the\s+temperature\b",  # Drop the temperature
    r"\bdid\s+i\s+lock\b",  # Did i lock the door
    r"\bhave\s+i\s+locked\b",  # Have i locked
    r"\bkill\s+all\b",  # Kill all the lights
    r"\bexcept\b",  # Exclusion patterns
    # Round 13 additions
    r"\bkinda\s+dim\b",  # It's kinda dim (needs brighter)
    r"\blooking\s+dim\b",  # Looking dim
    r"\bon\s+low\b",  # Lights on low
    r"\blight\s+going\b",  # Get the light going
    r"\bin\s+here\b",  # Purple in here (color with room context)
    r"\bwindow\s+open\b",  # Window open status
    r"\bwindows\s+open\b",  # Windows open status
    r"\bwhats\s+the\s+deal\b",  # Whats the deal with
    r"\bwhats\s+up\s+with\b",  # Whats up with
    r"\bwhats\s+jammin\b",  # Whats jamming (now playing)
    r"\bhold\s+up\b",  # Hold up (pause)
    r"\bcrank\s+this\b",  # Crank this up (volume)
    r"\bbring\s+them\s+back\s+up\b",  # Bring them back up (brightness)
    r"\bbring\s+it\s+back\s+up\b",  # Bring it back up
    # Round 14 additions
    r"\bmad\s+cold\b",  # Its mad cold (thermostat)
    r"\bmad\s+hot\b",  # Its mad hot (thermostat)
    r"\bdrop\s+that?\s+temp\b",  # Drop that temp
    r"\bweak\s+af\b",  # Lights weak af (brighter)
    r"\beverything\s+off\b",  # Everything off
    r"\bplayin\s+rn\b",  # Whats playin rn (now playing)
    r"\bmad\s+loud\b",  # Music mad loud (volume down)
    r"\bwarm\s+up\s+my\s+side\b",  # Warm up my side (bed)
    # Round 15: Occupancy queries (should never be cached)
    r"\banybody\s+in\b",  # Is there anybody in the X
    r"\banyone\s+in\b",  # Is there anyone in the X
    r"\bsomeone\s+in\b",  # Is there someone in the X
    r"\bis\s+there\s+anybody\b",  # Is there anybody
    r"\bis\s+there\s+anyone\b",  # Is there anyone
    # Round 15: Color requests
    r"\brandom\s+colors?\b",  # Random colors
    r"\bgimme\s+random\b",  # Gimme random
    r"\bgive\s+me\s+random\b",  # Give me random
    # Round 15: More patterns
    r"\bdoor\s+good\b",  # Door good (status check)
    r"\bnoise\s+down\b",  # Noise down (volume)
    r"\bflip\s+the\b",  # Flip the X (toggle)
    r"\bcrank\s+that\b",  # Crank that (volume up)
    r"\bset\s+the\s+mood\b",  # Set the mood
    r"\bless\s+volume\b",  # Less volume
    # Round 16: More patterns
    r"\bget\s+.*\s+lit\b",  # Get X lit (turn on)
    r"\blit\b",  # "lit" slang for on/bright
    r"\bsuper\s+bright\b",  # Super bright (max brightness)
    r"\blights\s+left\s+on\b",  # Any lights left on (status)
    r"\bany\s+lights\b",  # Any lights on (status)
    r"\bair\s+moving\b",  # Air moving (fan)
    r"\bchristmas\s+colors?\b",  # Christmas colors (light control)
    r"\bparty\s+vibes?\b",  # Party vibes (scene)
    r"\block\s+it\s+down\b",  # Lock it down
    r"\btemp\s+we\s+at\b",  # What temp we at
    # Round 19: Pronoun-based follow-ups - need conversation context
    r"\bwhat\s+(team|sport|position|city|state|country|year|age)\s+does\s+(he|she|they)\b",  # "what team does he play for"
    r"\bwhere\s+does\s+(he|she|they)\b",  # "where does he live"
    r"\bwhen\s+did\s+(he|she|they)\b",  # "when did he join"
    r"\bhow\s+(old|tall|much|many)\s+(is|was|are|were)\s+(he|she|they)\b",  # "how old is he"
    r"\bwho\s+is\s+(he|she|they)\b",  # "who is he"
    r"\b(he|she|him|her|his|they|them|their)\b.*\b(play|live|work|born|from)\b",  # Pronoun + action verbs

    # Round 21: Context-dependent follow-ups - NEVER cache
    r"^tell\s+me\s+more",  # "tell me more about X"
    r"\bthe\s+first\s+(one|story|option)\b",  # "the first story"
    r"\bthe\s+second\s+(one|story|option)\b",  # "the second one"
    r"\bthe\s+third\s+(one|story|option)\b",  # "the third one"
    r"^where\s+can\s+i\s+read\b",  # "where can I read more"
    r"^is\s+that\s+related\b",  # "is that related to X"
    r"^what\s+about\s+",  # "what about parking"
    r"^do\s+they\s+have\b",  # "do they have outdoor seating"
    r"^are\s+they\s+open\b",  # "are they open tomorrow"
    r"^whats?\s+their\s+",  # "what's their phone number"
    r"^whats?\s+the\s+price\b",  # "what's the price" (follow-up)
    r"^any\s+parking\b",  # "any parking nearby"
    r"^how\s+long\s+does\s+it\b",  # "how long does it take"
    r"^can\s+i\s+substitute\b",  # "can I substitute X"
    r"^what\s+should\s+i\s+serve\b",  # "what should I serve with it"
    r"\bfor\s+a\s+business\s+dinner\b",  # Context-dependent recommendation
    r"^which\s+one\s+",  # "which one is better"
    r"^which\s+would\s+you\b",  # "which would you pick"
    r"^which\s+has\s+better\b",  # "which has better reviews"
    r"^which\s+is\s+more\b",  # "which is more expensive"
    r"what\s+should\s+we\s+do\b",  # "what should we do" - activity suggestions
    r"what\s+should\s+i\s+do\b",  # "what should I do"
    r"if\s+its?\s+sunny\b",  # "if it's sunny" - conditional weather activity
    r"if\s+its?\s+rain",  # "if it rains" - conditional weather activity
    r"if\s+the\s+weather\b",  # "if the weather is good"
    # Round 21-30: General knowledge / hypothetical questions - never cache, use LLM directly
    r"\bhypothetically\b",  # Hypothetical scenarios
    r"\bwhat\s+if\s+i\s+(want|hate|only|just|find|gave|had)\b",  # "what if I wanted to..." - general advice
    r"\bhow\s+should\s+i\s+(train|learn|start|begin|prepare)\b",  # Training/learning advice
    r"\blets?\s+say\b",  # "let's say I..." - hypothetical
    r"\bassuming\s+i\b",  # "assuming I have..." - hypothetical context
    r"\bif\s+i\s+(only|just|hate|want|find|gave|had)\b",  # Conditional personal questions
    r"\bmarathon\b",  # Marathon training questions
    r"\bpersonal\s+trainer\b",  # Personal trainer questions
    r"\bget\s+fit\b",  # Fitness advice
    r"\bexercis(e|ing)\b",  # Exercise questions
    r"\bdiet\b",  # Diet questions
    r"\bworth\s+it\b",  # "is it worth it" - general advice
    r"\beasiest\s+way\b",  # "easiest way to..." - how-to questions
    r"\bwhat\s+about\s+iot\b",  # IoT questions
    r"\bzigbee\b",  # Technical protocol questions
    r"\bmqtt\b",  # MQTT protocol questions
    r"\bbandwidth\b",  # Network questions
    r"\blatency\b",  # Network latency questions
    # Round 21-30: Slang phrases that need LLM understanding
    r"\bwhats?\s+the\s+damage\b",  # "whats the damage" = price in slang
    r"\bdeadass\b",  # "deadass?" = "really?" confirmation slang
    r"\bno\s+cap\b",  # "no cap" = "no lie" / "for real"
    r"\bbet\b",  # "bet" = acknowledgment/agreement
    r"\bfinna\b",  # "finna" = "going to"
    r"\bbrick\b",  # "brick" = cold (weather slang)
    # Round 21-30: Emotional venting - should get empathetic response, not cached info
    r"\bwork\s+was\s+terrible\b",  # Emotional venting
    r"\btoday\s+sucked\b",  # Bad day venting
    r"\bbad\s+day\b",  # Bad day context
    r"\bi\s+just\s+want\s+comfort\b",  # Emotional comfort seeking
    r"\bugh\b",  # Frustration expression
    r"\bits?\s+raining\s+(and|again)\b",  # Emotional rain complaint (not weather query)
    # Round 21-30: False memory prevention - claims about "previous session"
    r"\bremember\s+that.*you\s+mentioned\b",  # False memory claim
    r"\blast\s+time\s+you\s+said\b",  # False memory claim
    r"\byou\s+told\s+me\s+(last|before|earlier)\b",  # False memory claim
    # Round 21-30: Context-dependent follow-ups
    r"\bwhere\s+was\s+that\s+restaurant\b",  # Needs session context
    r"\bwhat\s+was\s+the\s+name\s+again\b",  # Needs session context
    r"\bmy\s+uber\s+is\s+here\b",  # Urgent context-dependent
    # Round 21-30: Sarcastic/reaction words that shouldn't cache
    r"^shocking$",  # Sarcastic reaction, not a light command
    r"^surprised$",  # Sarcastic reaction
    r"\bmake\s+me\s+a\s+sandwich\b",  # Impossible request
    r"\bthanks\s+nerd\b",  # Casual thanks, not color command
    r"\bcool\s+thanks\b",  # Casual thanks, not color command
    r"\btomorrow\s+will\s+be\s+better\b",  # Reassurance, not schedule
    r"\bwill\s+be\s+better\s+right\b",  # Reassurance, not schedule
    r"\bcall\s+them\b",  # Impossible request (phone call)
    r"\bis\s+there\s+an\s+api\b",  # Technical question
    # Continuation requests - should never be cached, always fresh response
    r"\bcontinue\s+where\s+you\s+left\b",  # Continue where you left off
    r"\bplease\s+continue\b",  # Please continue
    r"\bkeep\s+going\b",  # Keep going
    r"\bcontinue\s+the\s+story\b",  # Continue the story
    r"\bwhat\s+happens\s+next\b",  # What happens next
    r"\bgo\s+on\b",  # Go on
    r"\btell\s+me\s+more\b",  # Tell me more
    r"\bfinish\s+(the|that|your)\b",  # Finish the story/that/your thought
]

# Location normalization for Baltimore area
LOCATION_NORMALIZATIONS = {
    "baltimore": "baltimore_md",
    "bmore": "baltimore_md",
    "charm city": "baltimore_md",
    "maryland": "baltimore_md",
    "md": "baltimore_md",
    "owings mills": "baltimore_md",
    "towson": "baltimore_md",
    "downtown": "baltimore_md",
}

# Phrases that indicate user is specifying a different location
# Note: [?!.;]* handles trailing punctuation like "in Philly?" or "near NYC!"
LOCATION_INDICATORS = [
    r'\bin\s+([a-zA-Z\s]+?)[?!.;]*(?:\s*,|\s*$|\s+(?:for|near|around|today|tonight|tomorrow))',
    r'\bnear\s+([a-zA-Z\s]+?)[?!.;]*(?:\s*,|\s*$|\s+(?:for|today|tonight|tomorrow))',
    r'\baround\s+([a-zA-Z\s]+?)[?!.;]*(?:\s*,|\s*$|\s+(?:for|today|tonight|tomorrow))',
    r'\bat\s+([a-zA-Z\s]+?)[?!.;]*(?:\s*,|\s*$|\s+(?:for|today|tonight|tomorrow))',
]


def normalize_location(text: str) -> str:
    """
    Normalize location references to canonical form.

    IMPORTANT: Only defaults to baltimore_md if NO location is specified.
    If user mentions a specific location (e.g., "in Northampton"), use that location
    to prevent cache collisions between different locations.
    """
    text_lower = text.lower()

    # First, check if user specified a location that's in our normalization dict
    for pattern, normalized in LOCATION_NORMALIZATIONS.items():
        if pattern in text_lower:
            return normalized

    # Check if user explicitly specified a different location
    # Extract the location name and use it as the cache key
    for pattern in LOCATION_INDICATORS:
        match = re.search(pattern, text_lower)
        if match:
            location = match.group(1).strip()
            # Clean up the location name for use as cache key
            if location and len(location) > 2:
                # Convert to a safe cache key format
                safe_location = re.sub(r'[^a-z0-9]+', '_', location.lower()).strip('_')
                if safe_location:
                    return safe_location

    # Only default to baltimore_md if truly no location is specified
    # Check for common phrases that imply default location
    default_phrases = ["around me", "near me", "nearby", "close by", "in my area", "local"]
    if any(phrase in text_lower for phrase in default_phrases):
        return "user_location"  # Different key than explicit baltimore queries

    return "baltimore_md"  # Default location when nothing is specified


def extract_semantic_intent(query: str) -> Tuple[str, str]:
    """
    Extract semantic intent category and normalized query from raw query.

    Returns:
        (category, normalized_query) - category for TTL lookup, normalized query for cache key
    """
    query_lower = query.lower().strip()

    # Recipes - check FIRST to catch "make dinner with chicken" BEFORE dining matches "dinner"
    recipe_patterns = [
        "recipe", "how to make", "how to cook", "ingredients for",
        "what can i make with", "make dinner with", "make lunch with",
        "cook something with", "prepare dinner", "prepare lunch",
        "i want to make", "want to cook", "need to cook", "should i cook",
        "something to make with", "ideas for cooking"
    ]
    if any(p in query_lower for p in recipe_patterns):
        # Try to extract dish or main ingredient
        dish_match = re.search(r'(?:recipe for|how to (?:make|cook)|make (?:dinner|lunch) with|with) (.+?)(?:\?|$)', query_lower)
        dish = dish_match.group(1).strip().replace(" ", "_")[:30] if dish_match else "general"
        return ("recipes", f"recipe_{dish}")

    # Weather queries
    if any(w in query_lower for w in ["weather", "temperature", "forecast", "rain", "sunny", "cold", "hot"]):
        location = normalize_location(query_lower)
        # Normalize variations: "what's the weather" == "how's the weather" == "weather"
        return ("weather", f"weather_{location}")

    # Dining queries - expanded patterns for natural language
    dining_patterns = [
        "restaurant", "where to eat", "food near", "dinner", "lunch", "breakfast", "dining",
        "place to eat", "eat tonight", "eat today", "good place", "recommend a", "recommendation",
        "somewhere to eat", "grab a bite", "get food", "hungry", "cuisine"
    ]
    # Also match cuisine names directly (e.g., "good Greek place")
    cuisine_triggers = ["greek", "italian", "mexican", "chinese", "japanese", "thai", "indian",
                        "american", "sushi", "pizza", "burger", "korean", "vietnamese", "french",
                        "mediterranean", "seafood", "steakhouse", "bbq", "barbecue", "jamaican",
                        "irish", "spanish", "cuban", "brazilian", "peruvian", "ethiopian", "moroccan",
                        "turkish", "lebanese", "german", "british", "southern", "cajun", "soul food",
                        "vegan", "vegetarian", "ramen", "pho", "dim sum", "tapas"]

    is_dining = any(w in query_lower for w in dining_patterns)
    # Check if a cuisine type is mentioned with eating-related context
    if not is_dining:
        for cuisine_word in cuisine_triggers:
            if cuisine_word in query_lower and any(eat_word in query_lower for eat_word in ["place", "spot", "eat", "food", "tonight", "today", "near"]):
                is_dining = True
                break

    if is_dining:
        location = normalize_location(query_lower)
        # Extract cuisine type if present
        cuisine = "general"
        for c in cuisine_triggers:
            if c in query_lower:
                cuisine = c
                break
        return ("dining", f"dining_{location}_{cuisine}")

    # Sports queries - more granular cache keys to avoid returning wrong cached data
    sports_keywords = ["game", "score", "ravens", "orioles", "nfl", "mlb", "nba", "nhl", "match",
                       "playoff", "standings", "bracket", "season", "championship", "super bowl"]
    if any(w in query_lower for w in sports_keywords):
        # Determine league
        league = "general"
        for l in ["nfl", "nba", "mlb", "nhl", "ncaa", "mls"]:
            if l in query_lower:
                league = l
                break

        # Determine query type (more specific cache keys)
        query_type = "scores"  # default
        if any(w in query_lower for w in ["playoff", "bracket", "picture", "wild card", "seed"]):
            query_type = "playoff"
        elif any(w in query_lower for w in ["standing", "rank", "division", "conference", "record"]):
            query_type = "standings"
        elif any(w in query_lower for w in ["schedule", "upcoming", "next game", "when do"]):
            query_type = "schedule"
        elif any(w in query_lower for w in ["latest", "recent", "last game", "yesterday"]):
            query_type = "recent"

        # Determine team (most specific)
        team = "all"
        for t in ["ravens", "orioles", "commanders", "nationals", "wizards", "capitals",
                  "eagles", "cowboys", "giants", "steelers", "chiefs", "bills", "49ers"]:
            if t in query_lower:
                team = t
                break

        return ("sports", f"sports_{league}_{query_type}_{team}")

    # News queries
    if any(w in query_lower for w in ["news", "headline", "what's happening"]):
        return ("news", "news_current")

    # Stocks queries
    if any(w in query_lower for w in ["stock", "market", "price of", "how is", "nasdaq", "dow"]):
        # Extract ticker if present
        ticker = re.search(r'\b([A-Z]{2,5})\b', query)
        ticker_key = ticker.group(1).lower() if ticker else "market"
        return ("stocks", f"stocks_{ticker_key}")

    # Time queries - NEVER CACHE
    if any(w in query_lower for w in ["time", "date", "day is it"]):
        return ("time", "")

    # Smart home - NEVER CACHE
    if any(w in query_lower for w in ["turn", "set temperature", "lights", "thermostat", "lock", "unlock"]):
        return ("smart_home", "")

    # Events
    if any(w in query_lower for w in ["events", "happening", "concerts", "shows", "tickets"]):
        location = normalize_location(query_lower)
        return ("events", f"events_{location}")

    # Flights/Airports
    if any(w in query_lower for w in ["flight", "airport", "departures", "arrivals", "bwi"]):
        return ("airports", "airports_bwi")

    # Streaming
    if any(w in query_lower for w in ["watch", "netflix", "hulu", "streaming", "movie", "show"]):
        return ("streaming", "streaming_general")

    # Directions queries - MUST include origin location in cache key
    # These are location-sensitive and should have different cache entries per origin
    directions_patterns = [
        "directions", "how do i get to", "how to get to", "navigate to",
        "route to", "drive to", "driving to", "way to", "fastest route",
        "how far", "how long to get", "trip to", "going to"
    ]
    if any(w in query_lower for w in directions_patterns):
        # Extract destination if possible
        dest_match = re.search(r'(?:to|get to|reach|navigate to)\s+(.+?)(?:\?|$|from)', query_lower)
        dest = dest_match.group(1).strip().replace(" ", "_")[:30] if dest_match else "unknown"
        # Note: Origin will be added via location_override parameter in get_cached_response
        return ("directions", f"directions_to_{dest}")

    # Default - general queries
    return ("general", hashlib.md5(query_lower.encode()).hexdigest()[:16])


def is_cacheable(category: str, query: str) -> bool:
    """Check if a query should be cached."""
    # Check category TTL - 0 means never cache
    ttl = CACHE_TTL_CONFIG.get(category, 0)
    if ttl == 0:
        return False

    # Check for uncacheable patterns
    query_lower = query.lower()
    for pattern in UNCACHEABLE_PATTERNS:
        if re.search(pattern, query_lower):
            logger.info("cache_skip_pattern", pattern=pattern, query_preview=query[:50])
            return False

    return True


def get_cache_key(normalized_query: str, room: str = None, mode: str = None, location_override: dict = None) -> str:
    """Generate cache key with optional room/mode/location context."""
    key_parts = ["athena_semantic", normalized_query]

    # Include location_override in cache key for location-sensitive queries (directions, dining, etc.)
    # This ensures different origins get different cache entries
    if location_override:
        if location_override.get("address"):
            # Use address hash for consistent key
            loc_hash = hashlib.md5(location_override["address"].encode()).hexdigest()[:8]
            key_parts.append(f"loc_{loc_hash}")
        elif location_override.get("latitude") and location_override.get("longitude"):
            # Use coordinates rounded to ~1km precision
            lat = round(location_override["latitude"], 2)
            lon = round(location_override["longitude"], 2)
            key_parts.append(f"loc_{lat}_{lon}")

    return ":".join(key_parts)


async def get_cached_response(query: str, room: str = None, mode: str = None, location_override: dict = None) -> Optional[Dict[str, Any]]:
    """
    Check cache for a semantically similar query.

    Args:
        query: User query
        room: Optional room context
        mode: Optional user mode
        location_override: Optional location override dict with address, latitude, longitude

    Returns:
        Cached response dict if found and valid, None otherwise
    """
    category, normalized_query = extract_semantic_intent(query)

    # Check if this query type is cacheable
    if not is_cacheable(category, query):
        logger.debug("semantic_cache_skip", category=category, reason="not_cacheable")
        return None

    cache_key = get_cache_key(normalized_query, room, mode, location_override)
    cache = get_cache_client()

    try:
        cached = await cache.get(cache_key)
        if cached:
            logger.info(
                "semantic_cache_hit",
                category=category,
                cache_key=cache_key[:50],
                query_preview=query[:30],
                has_location=bool(location_override)
            )
            return cached

        logger.debug("semantic_cache_miss", cache_key=cache_key[:50], has_location=bool(location_override))
        return None

    except Exception as e:
        logger.warning("semantic_cache_get_error", error=str(e))
        return None


async def cache_response(
    query: str,
    response: Dict[str, Any],
    room: str = None,
    mode: str = None,
    location_override: dict = None
) -> bool:
    """
    Cache a query response with appropriate TTL based on intent category.

    Args:
        query: Original user query
        response: Response dict to cache (must be JSON-serializable)
        room: Optional room context
        mode: Optional user mode
        location_override: Optional location override dict with address, latitude, longitude

    Returns:
        True if cached successfully, False otherwise
    """
    category, normalized_query = extract_semantic_intent(query)

    # Check if this query type is cacheable
    if not is_cacheable(category, query):
        return False

    # Get TTL for this category
    ttl = CACHE_TTL_CONFIG.get(category, CACHE_TTL_CONFIG["general"])
    cache_key = get_cache_key(normalized_query, room, mode, location_override)
    cache = get_cache_client()

    try:
        # Add metadata for cache debugging
        cached_response = {
            **response,
            "_cache_metadata": {
                "category": category,
                "normalized_query": normalized_query,
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": ttl
            }
        }

        await cache.set(cache_key, cached_response, ttl=ttl)

        logger.info(
            "semantic_cache_stored",
            category=category,
            cache_key=cache_key[:50],
            ttl_seconds=ttl,
            query_preview=query[:30]
        )
        return True

    except Exception as e:
        logger.warning("semantic_cache_set_error", error=str(e))
        return False


async def invalidate_cache(category: str = None, pattern: str = None) -> int:
    """
    Invalidate cached responses by category or pattern.

    Args:
        category: Invalidate all caches for this category (e.g., "weather")
        pattern: Invalidate caches matching this pattern

    Returns:
        Number of keys invalidated
    """
    cache = get_cache_client()

    try:
        if category:
            pattern = f"athena_semantic:{category}_*"
        elif not pattern:
            pattern = "athena_semantic:*"

        # Note: This requires SCAN which may not be available in all Redis configs
        # For now, just log the intent
        logger.info("semantic_cache_invalidate_requested", pattern=pattern)

        # Would need: keys = await cache.client.keys(pattern)
        # For each key: await cache.delete(key)

        return 0  # Placeholder - full implementation needs Redis SCAN

    except Exception as e:
        logger.warning("semantic_cache_invalidate_error", error=str(e))
        return 0
